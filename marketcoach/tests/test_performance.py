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


class TestPerformanceSpyBenchmark:
    """
    SPY benchmark series answers "did active trading actually beat
    buy-and-hold SPY over the same windows?" Computed per-trade as
    "notional * (SPY close-at-trade-close / SPY close-at-trade-open - 1)"
    and cumulatively summed — apples-to-apples with the realised P&L
    curve which runs on the same windows and same capital.
    """

    def _mock_spy_closes(self, mapping: dict):
        """Patch the module-level cache directly so tests don't hit yfinance."""
        from datetime import datetime, timezone

        from backend.main import _SPY_CACHE
        _SPY_CACHE["fetched_at"] = datetime.now(timezone.utc)
        _SPY_CACHE["closes"] = mapping

    def _reset_spy_cache(self):
        from backend.main import _SPY_CACHE
        _SPY_CACHE["fetched_at"] = None
        _SPY_CACHE["closes"] = {}

    def test_empty_benchmark_on_no_trades(self, client):
        self._reset_spy_cache()
        body = client.get("/performance").json()
        assert body["spy_benchmark"] == []

    def test_benchmark_excluded_when_yfinance_fails(self, client, db):
        """
        yfinance unreachable → the page still renders, just without the
        comparison line. Benchmark defaults to [] rather than crashing
        the whole endpoint.
        """
        self._reset_spy_cache()
        _seed_trade(
            db, "NVDA", 10, 100.0, 110.0,
            pnl=100.0, pnl_pct=10.0, days_held=1,
        )
        from unittest.mock import patch as _patch
        with _patch("backend.main._fetch_spy_closes", return_value={}):
            body = client.get("/performance").json()
        assert body["spy_benchmark"] == []
        # Real equity curve still computes
        assert len(body["equity_curve"]) == 1

    def test_benchmark_cumulative_with_known_spy_series(self, client, db):
        """
        Two closed trades against a fabricated SPY series with known
        returns. Verifies the per-trade math and the cumulative running
        sum end-to-end.
        """
        now = datetime.now(timezone.utc)
        # Trade 1: opened 10d ago, closed 5d ago. SPY went 400 → 420 (+5%).
        t1_open = now - timedelta(days=10)
        t1_close = now - timedelta(days=5)
        # Trade 2: opened 3d ago, closed 1d ago. SPY went 420 → 410 (-2.38%).
        t2_open = now - timedelta(days=3)
        t2_close = now - timedelta(days=1)

        _seed_trade(
            db, "NVDA", 10, 100.0, 120.0,
            pnl=200.0, pnl_pct=20.0, days_held=5,
            opened_at=t1_open, closed_at=t1_close,
        )
        _seed_trade(
            db, "SPY", 5, 200.0, 190.0,
            pnl=-50.0, pnl_pct=-5.0, days_held=2,
            opened_at=t2_open, closed_at=t2_close,
        )

        self._mock_spy_closes({
            t1_open.date().isoformat(): 400.0,
            t1_close.date().isoformat(): 420.0,
            t2_open.date().isoformat(): 420.0,
            t2_close.date().isoformat(): 410.0,
        })

        body = client.get("/performance").json()
        bench = body["spy_benchmark"]
        assert len(bench) == 2

        # Trade 1: notional = 10 * 100 = 1000; SPY +5% → +50
        assert bench[0]["trade_spy_pnl"] == pytest.approx(50.0, abs=0.01)
        assert bench[0]["cumulative_spy_pnl"] == pytest.approx(50.0, abs=0.01)

        # Trade 2: notional = 5 * 200 = 1000; SPY -2.381% → ~-23.81
        # Cumulative: 50 - 23.81 = 26.19
        assert bench[1]["trade_spy_pnl"] == pytest.approx(-23.81, abs=0.05)
        assert bench[1]["cumulative_spy_pnl"] == pytest.approx(26.19, abs=0.05)

    def test_weekend_close_dates_resolve_to_prior_trading_day(self, client, db):
        """
        Weekend/holiday dates aren't in the SPY series. The helper walks
        back up to 7 days to find the most recent trading day. Without
        this, weekend-closed trades would silently drop from the benchmark.
        """
        now = datetime.now(timezone.utc)
        t_open = now - timedelta(days=10)
        t_close = now - timedelta(days=3)

        _seed_trade(
            db, "NVDA", 10, 100.0, 110.0,
            pnl=100.0, pnl_pct=10.0, days_held=7,
            opened_at=t_open, closed_at=t_close,
        )

        # Only the open date + a date 2 days before the close are in the
        # series. The walk-back should resolve the close to that earlier
        # trading day.
        self._mock_spy_closes({
            t_open.date().isoformat(): 400.0,
            (t_close - timedelta(days=2)).date().isoformat(): 420.0,
        })

        bench = client.get("/performance").json()["spy_benchmark"]
        assert len(bench) == 1
        assert bench[0]["trade_spy_pnl"] != 0

    def test_benchmark_skips_trade_with_missing_spy_data(self, client, db):
        """
        One trade with coverage + one trade with dates outside the SPY
        series → benchmark has the one, skips the other, never crashes.
        """
        now = datetime.now(timezone.utc)
        t1_open = now - timedelta(days=10)
        t1_close = now - timedelta(days=5)
        t2_open = now - timedelta(days=300)
        t2_close = now - timedelta(days=295)

        _seed_trade(
            db, "HAS_DATA", 10, 100.0, 110.0,
            pnl=100.0, pnl_pct=10.0, days_held=5,
            opened_at=t1_open, closed_at=t1_close,
        )
        _seed_trade(
            db, "NO_DATA", 10, 100.0, 110.0,
            pnl=100.0, pnl_pct=10.0, days_held=5,
            opened_at=t2_open, closed_at=t2_close,
        )

        self._mock_spy_closes({
            t1_open.date().isoformat(): 400.0,
            t1_close.date().isoformat(): 420.0,
        })

        bench = client.get("/performance").json()["spy_benchmark"]
        assert len(bench) == 1
        assert bench[0]["ticker"] == "HAS_DATA"

    def test_benchmark_excludes_paper_trades(self, client, db):
        """Live-only filter applies to the SPY benchmark too."""
        now = datetime.now(timezone.utc)
        t1_open = now - timedelta(days=5)
        t1_close = now - timedelta(days=1)

        _seed_trade(
            db, "LIVE", 10, 100.0, 110.0,
            pnl=100.0, pnl_pct=10.0, days_held=4,
            opened_at=t1_open, closed_at=t1_close,
            is_paper=False,
        )
        _seed_trade(
            db, "PAPER", 10, 100.0, 200.0,
            pnl=1000.0, pnl_pct=100.0, days_held=4,
            opened_at=t1_open, closed_at=t1_close,
            is_paper=True,
        )

        self._mock_spy_closes({
            t1_open.date().isoformat(): 400.0,
            t1_close.date().isoformat(): 420.0,
        })

        bench = client.get("/performance").json()["spy_benchmark"]
        assert len(bench) == 1
        assert bench[0]["ticker"] == "LIVE"


class TestSpyCloseResolver:
    """Direct tests of the _spy_close_on_or_before walk-back helper."""

    def test_exact_match(self):
        from backend.main import _spy_close_on_or_before
        closes = {"2026-04-15": 500.0, "2026-04-14": 498.0}
        assert _spy_close_on_or_before("2026-04-15", closes) == 500.0

    def test_walks_back_to_prior_trading_day(self):
        from backend.main import _spy_close_on_or_before
        # Saturday 2026-04-18 not in series; should resolve to Friday
        closes = {"2026-04-17": 500.0, "2026-04-16": 495.0}
        assert _spy_close_on_or_before("2026-04-18", closes) == 500.0

    def test_returns_none_when_no_match_within_window(self):
        from backend.main import _spy_close_on_or_before
        # Date is 100+ days after anything in the series — give up
        closes = {"2026-01-01": 500.0}
        assert _spy_close_on_or_before("2026-04-15", closes) is None

    def test_invalid_date_string_returns_none(self):
        from backend.main import _spy_close_on_or_before
        assert _spy_close_on_or_before("not-a-date", {"2026-04-15": 500.0}) is None
