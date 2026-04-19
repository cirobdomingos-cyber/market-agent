"""
Tests for the /performance endpoint.

Scope: verifies every KPI computes correctly against a known fixture
DB of closed trades. Uses real TradeJournalEntry + ExecutedOrder rows
with the is_paper flag set to simulate the live-only filter.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import get_db
from backend.db.models import Base, ExecutedOrder, TradeJournalEntry
from backend.main import app


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine)


@pytest.fixture(autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def db():
    s = _TestSession()
    yield s
    s.close()


@pytest.fixture()
def client(db):
    def override():
        yield db
    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_trade(
    db,
    ticker: str,
    qty: float,
    open_price: float,
    close_price: float | None = None,
    pnl: float | None = None,
    pnl_pct: float | None = None,
    days_held: int | None = None,
    status: str = "closed",
    is_paper: bool = False,
    advisor_session_id: str | None = None,
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
):
    """
    Build a linked ExecutedOrder + TradeJournalEntry pair. Defaults match
    a closed live BUY trade. Override is_paper=True to test filtering.
    """
    if opened_at is None:
        opened_at = datetime.now(timezone.utc) - timedelta(days=days_held or 0)
    if closed_at is None and status == "closed":
        closed_at = datetime.now(timezone.utc)

    order = ExecutedOrder(
        ticker=ticker,
        side="buy",
        qty=qty,
        order_type="limit",
        limit_price=open_price,
        status="filled",
        is_paper=is_paper,
        submitted_at=opened_at,
        filled_at=opened_at,
    )
    db.add(order)
    db.flush()

    entry = TradeJournalEntry(
        open_executed_order_id=order.id,
        ticker=ticker,
        side="buy",
        qty=qty,
        open_price=open_price,
        opened_at=opened_at,
        close_price=close_price,
        closed_at=closed_at,
        pnl_amount=pnl,
        pnl_pct=pnl_pct,
        days_held=days_held,
        status=status,
        user_thesis="Test thesis long enough to be valid",
        advisor_session_id=advisor_session_id,
    )
    db.add(entry)
    db.commit()
    return entry


class TestPerformanceEmpty:
    """With no data, every KPI returns a sensible null/zero."""

    def test_empty_db_returns_zero_shell(self, client):
        resp = client.get("/performance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["closed_trades_count"] == 0
        assert body["open_positions_count"] == 0
        assert body["win_rate_pct"] is None
        assert body["total_realized_pnl"] == 0.0
        assert body["profit_factor"] is None
        assert body["recent_trades"] == []


class TestPerformanceLiveOnlyFilter:
    """Paper trades must be excluded — they don't count toward live perf."""

    def test_paper_trades_are_excluded(self, client, db):
        _seed_trade(db, "PAPER1", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=5, is_paper=True)
        _seed_trade(db, "PAPER2", 10, 100.0, 90.0, pnl=-100.0, pnl_pct=-10.0, days_held=3, is_paper=True)

        resp = client.get("/performance")
        body = resp.json()
        assert body["closed_trades_count"] == 0

    def test_mixed_paper_and_live_counts_only_live(self, client, db):
        _seed_trade(db, "LIVE1", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=5, is_paper=False)
        _seed_trade(db, "PAPER1", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=5, is_paper=True)
        _seed_trade(db, "LIVE2", 10, 100.0, 90.0, pnl=-100.0, pnl_pct=-10.0, days_held=3, is_paper=False)

        resp = client.get("/performance")
        body = resp.json()
        assert body["closed_trades_count"] == 2
        assert body["winners_count"] == 1
        assert body["losers_count"] == 1
        assert body["total_realized_pnl"] == 0.0  # +100 - 100

    def test_open_positions_filtered_to_live(self, client, db):
        _seed_trade(db, "OPEN_LIVE", 10, 100.0, status="open", is_paper=False)
        _seed_trade(db, "OPEN_PAPER", 10, 100.0, status="open", is_paper=True)

        resp = client.get("/performance")
        body = resp.json()
        assert body["open_positions_count"] == 1
        assert body["closed_trades_count"] == 0


class TestPerformanceKpiComputation:
    """Each KPI against a known 3-trade fixture: 2 winners + 1 loser."""

    def _seed_standard_three(self, db):
        # Winner +$200 (10%, held 3 days)
        _seed_trade(db, "WIN1", 10, 100.0, 120.0, pnl=200.0, pnl_pct=20.0, days_held=3)
        # Winner +$50 (5%, held 10 days) — smaller win
        _seed_trade(db, "WIN2", 10, 100.0, 105.0, pnl=50.0, pnl_pct=5.0, days_held=10)
        # Loser -$100 (-10%, held 5 days)
        _seed_trade(db, "LOSE1", 10, 100.0, 90.0, pnl=-100.0, pnl_pct=-10.0, days_held=5)

    def test_win_rate(self, client, db):
        self._seed_standard_three(db)
        body = client.get("/performance").json()
        # 2 winners of 3 = 66.7%
        assert body["win_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_totals_and_averages(self, client, db):
        self._seed_standard_three(db)
        body = client.get("/performance").json()
        assert body["closed_trades_count"] == 3
        assert body["winners_count"] == 2
        assert body["losers_count"] == 1
        assert body["total_realized_pnl"] == pytest.approx(150.0)  # 200 + 50 - 100
        # Avg win = (200 + 50) / 2 = 125
        assert body["avg_win_dollar"] == pytest.approx(125.0)
        # Avg loss = -100 / 1 = -100 (negative by convention)
        assert body["avg_loss_dollar"] == pytest.approx(-100.0)

    def test_largest_win_and_loss(self, client, db):
        self._seed_standard_three(db)
        body = client.get("/performance").json()
        assert body["largest_win"] == pytest.approx(200.0)
        assert body["largest_loss"] == pytest.approx(-100.0)

    def test_profit_factor(self, client, db):
        self._seed_standard_three(db)
        body = client.get("/performance").json()
        # gross_wins / gross_losses = 250 / 100 = 2.5
        assert body["profit_factor"] == pytest.approx(2.5)

    def test_profit_factor_none_when_no_losers(self, client, db):
        # Only winners — profit factor undefined (division by zero)
        _seed_trade(db, "W1", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1)
        _seed_trade(db, "W2", 10, 100.0, 120.0, pnl=200.0, pnl_pct=20.0, days_held=2)
        body = client.get("/performance").json()
        assert body["profit_factor"] is None
        # Other fields still compute
        assert body["win_rate_pct"] == 100.0
        assert body["avg_loss_dollar"] is None
        assert body["largest_loss"] is None

    def test_avg_hold_days(self, client, db):
        self._seed_standard_three(db)
        body = client.get("/performance").json()
        # (3 + 10 + 5) / 3 = 6.0
        assert body["avg_hold_days"] == pytest.approx(6.0)


class TestPerformanceAdvisorAttribution:
    """
    Trades linked to an advisor_session_id get counted separately. Lets the
    user answer: "are my advisor-sourced trades doing better than my gut
    ones?"
    """

    def test_no_advised_trades_sets_none(self, client, db):
        _seed_trade(db, "GUT", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1)
        body = client.get("/performance").json()
        assert body["advised_trades_count"] == 0
        assert body["advised_win_rate_pct"] is None

    def test_advised_win_rate_computed(self, client, db):
        _seed_trade(
            db, "AD_WIN", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1,
            advisor_session_id="advisor-1",
        )
        _seed_trade(
            db, "AD_LOSE", 10, 100.0, 90.0, pnl=-100.0, pnl_pct=-10.0, days_held=1,
            advisor_session_id="advisor-2",
        )
        # Gut trade — should NOT count toward advised stats
        _seed_trade(db, "GUT", 10, 100.0, 120.0, pnl=200.0, pnl_pct=20.0, days_held=1)

        body = client.get("/performance").json()
        assert body["advised_trades_count"] == 2
        assert body["advised_win_rate_pct"] == 50.0
        # Overall win rate = all three, 2 winners
        assert body["closed_trades_count"] == 3
        assert body["win_rate_pct"] == pytest.approx(66.7, abs=0.1)


class TestPerformanceRecentTrades:
    """The table view payload — most recent 50, newest first."""

    def test_recent_trades_includes_expected_fields(self, client, db):
        _seed_trade(
            db, "NVDA", 5, 500.0, 520.0,
            pnl=100.0, pnl_pct=4.0, days_held=2,
            advisor_session_id="session-xyz",
        )
        body = client.get("/performance").json()
        t = body["recent_trades"][0]
        assert t["ticker"] == "NVDA"
        assert t["qty"] == 5
        assert t["open_price"] == 500.0
        assert t["close_price"] == 520.0
        assert t["pnl_amount"] == 100.0
        assert t["pnl_pct"] == 4.0
        assert t["days_held"] == 2
        assert t["advisor_session_id"] == "session-xyz"
        assert "Test thesis" in t["user_thesis"]

    def test_recent_trades_ordered_newest_first(self, client, db):
        now = datetime.now(timezone.utc)
        _seed_trade(
            db, "OLD", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1,
            closed_at=now - timedelta(days=5),
        )
        _seed_trade(
            db, "NEW", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1,
            closed_at=now,
        )
        body = client.get("/performance").json()
        tickers = [t["ticker"] for t in body["recent_trades"]]
        assert tickers == ["NEW", "OLD"]

    def test_recent_trades_capped_at_50(self, client, db):
        for i in range(60):
            _seed_trade(
                db, f"T{i}", 1, 100.0, 110.0,
                pnl=10.0, pnl_pct=10.0, days_held=1,
            )
        body = client.get("/performance").json()
        assert len(body["recent_trades"]) == 50
        assert body["closed_trades_count"] == 60


class TestPerformanceEquityCurve:
    """
    Cumulative realised P&L series — one point per closed trade, ordered
    oldest first so the chart reads left-to-right, each point carrying
    the running sum after that trade settled.
    """

    def test_empty_curve_on_no_trades(self, client):
        body = client.get("/performance").json()
        assert body["equity_curve"] == []

    def test_curve_is_cumulative_and_oldest_first(self, client, db):
        now = datetime.now(timezone.utc)
        # Seed in reverse chronological order to verify sorting
        _seed_trade(
            db, "NEW", 10, 100.0, 90.0, pnl=-100.0, pnl_pct=-10.0, days_held=1,
            closed_at=now,
        )
        _seed_trade(
            db, "MID", 10, 100.0, 120.0, pnl=200.0, pnl_pct=20.0, days_held=1,
            closed_at=now - timedelta(days=3),
        )
        _seed_trade(
            db, "OLD", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1,
            closed_at=now - timedelta(days=5),
        )

        curve = client.get("/performance").json()["equity_curve"]
        assert len(curve) == 3
        # Oldest first: OLD (+100), MID (+200), NEW (-100)
        assert [p["ticker"] for p in curve] == ["OLD", "MID", "NEW"]
        # Cumulative: 100, 300, 200
        assert curve[0]["cumulative_pnl"] == 100.0
        assert curve[1]["cumulative_pnl"] == 300.0
        assert curve[2]["cumulative_pnl"] == 200.0
        # Each point also carries its own contribution
        assert curve[0]["trade_pnl"] == 100.0
        assert curve[1]["trade_pnl"] == 200.0
        assert curve[2]["trade_pnl"] == -100.0

    def test_curve_excludes_paper_trades(self, client, db):
        """Live-only filter applies to the curve too."""
        _seed_trade(
            db, "LIVE", 10, 100.0, 110.0, pnl=100.0, pnl_pct=10.0, days_held=1,
            is_paper=False,
        )
        _seed_trade(
            db, "PAPER", 10, 100.0, 200.0, pnl=1000.0, pnl_pct=100.0, days_held=1,
            is_paper=True,
        )
        curve = client.get("/performance").json()["equity_curve"]
        assert len(curve) == 1
        assert curve[0]["ticker"] == "LIVE"
