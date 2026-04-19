"""
Tests for the order execution path.

The /orders/confirm endpoint is the most consequential thing in the codebase
— it's the only path that actually moves money. These tests cover every
safety gate, every rejection reason, and the persistence audit trail.

The broker client is mocked at the get_broker level so we never
hit the real Alpaca/IBKR API.
"""

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings, settings
from backend.db import crud, get_db
from backend.db.models import Base
from backend.main import app
from backend.brokers import OrderResult


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine)


_DEFAULT_TEST_WATCHLIST = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META", "SPY", "QQQ"]


def _seed_watchlist(session, tickers):
    """Clear + reseed the watchlist for a test. The test DB is fresh per
    test, so this is the fastest way to set a specific whitelist."""
    from backend.db.models import WatchlistTicker
    session.query(WatchlistTicker).delete()
    for t in tickers:
        session.add(WatchlistTicker(ticker=t.upper()))
    session.commit()


@pytest.fixture(autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=_engine)
    # Seed the production-default watchlist before every test so NVDA/etc.
    # pass the whitelist gate by default. Tests that need a custom watchlist
    # override it with _seed_watchlist(db, [...]).
    session = _TestSession()
    try:
        _seed_watchlist(session, _DEFAULT_TEST_WATCHLIST)
    finally:
        session.close()
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


@pytest.fixture()
def mock_alpaca():
    """Build a mocked Alpaca client that always succeeds with a filled order."""
    mock = MagicMock()
    mock.get_positions.return_value = []
    mock.get_account.return_value = {
        "equity": 100_000.0,
        "buying_power": 200_000.0,
        "cash": 100_000.0,
        "portfolio_value": 100_000.0,
        "paper": True,
    }
    mock.place_order.return_value = OrderResult(
        order_id="alpaca-order-123",
        ticker="NVDA",
        qty=10.0,
        side="buy",
        status="filled",
        is_paper=True,
        fill_price=450.12,
    )
    return mock


def _valid_request(**overrides):
    """Build a valid order request body. NVDA is in the default watchlist."""
    base = {
        "ticker": "NVDA",
        "side": "buy",
        "qty": 10,
        "order_type": "limit",
        "limit_price": 450.00,
        "rationale": "Bullish breakout test",
        "advisor_session_id": "advisor-test",
        "confirm_live_capital": False,
        "user_thesis": "Test thesis: NVDA breakout looks clean on volume",
        "user_disagreement": None,
    }
    base.update(overrides)
    return base


# ── Schema validation ──────────────────────────────────────────────────────

class TestSchemaValidation:
    def test_rejects_lowercase_ticker(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(ticker="nvda"))
        assert resp.status_code == 422

    def test_rejects_long_ticker(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(ticker="ABCDEFG"))
        assert resp.status_code == 422

    def test_rejects_invalid_side(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(side="hodl"))
        assert resp.status_code == 422

    def test_rejects_zero_qty(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(qty=0))
        assert resp.status_code == 422

    def test_rejects_negative_qty(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(qty=-5))
        assert resp.status_code == 422

    def test_rejects_excessive_qty(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(qty=20_000))
        assert resp.status_code == 422

    def test_rejects_invalid_order_type(self, client):
        resp = client.post("/orders/confirm", json=_valid_request(order_type="stop"))
        assert resp.status_code == 422

    def test_rejects_limit_without_price(self, client):
        resp = client.post(
            "/orders/confirm",
            json=_valid_request(order_type="limit", limit_price=None),
        )
        assert resp.status_code == 422
        assert "limit_price" in resp.json()["detail"].lower()

    def test_rejects_market_with_price(self, client):
        resp = client.post(
            "/orders/confirm",
            json=_valid_request(order_type="market", limit_price=450.00),
        )
        assert resp.status_code == 422


# ── Live mode gate ─────────────────────────────────────────────────────────

class TestLiveModeGate:
    def test_paper_mode_does_not_require_confirmation(self, client, mock_alpaca, monkeypatch):
        monkeypatch.setattr(settings, "alpaca_paper", True)
        monkeypatch.setattr(settings, "alpaca_live_confirmation", "")

        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(confirm_live_capital=False),
            )
        # Should pass live-mode check (we're in paper)
        assert resp.status_code == 200

    def test_live_mode_blocks_without_confirmation(self, client, mock_alpaca, monkeypatch):
        monkeypatch.setattr(settings, "alpaca_paper", False)
        monkeypatch.setattr(
            settings,
            "alpaca_live_confirmation",
            Settings.LIVE_CONFIRMATION_PHRASE,
        )

        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(confirm_live_capital=False),
            )
        assert resp.status_code == 403
        assert "live mode" in resp.json()["detail"].lower()

    def test_live_mode_allows_with_confirmation(self, client, mock_alpaca, monkeypatch):
        monkeypatch.setattr(settings, "alpaca_paper", False)
        monkeypatch.setattr(
            settings,
            "alpaca_live_confirmation",
            Settings.LIVE_CONFIRMATION_PHRASE,
        )

        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(confirm_live_capital=True),
            )
        assert resp.status_code == 200


# ── Connection gate ────────────────────────────────────────────────────────

class TestConnectionGate:
    def test_no_alpaca_client_returns_503(self, client):
        with patch("backend.main.get_broker", return_value=None):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 503
        assert "alpaca" in resp.json()["detail"].lower()


# ── Daily cap gate ─────────────────────────────────────────────────────────

class TestDailyCap:
    def test_cap_enforced_after_threshold(self, client, db, mock_alpaca, monkeypatch):
        # Pre-populate executed_orders with 20 entries from today
        from backend.main import ORDER_DAILY_CAP
        for i in range(ORDER_DAILY_CAP):
            crud.create_executed_order(
                db,
                ticker="NVDA",
                side="buy",
                qty=1,
                order_type="market",
                status="filled",
                is_paper=True,
            )

        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())

        # The 21st attempt is rejected (with persisted row, status 200 + body)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "daily order cap" in body["rejection_reason"].lower()


# ── Whitelist gate ─────────────────────────────────────────────────────────

class TestWhitelistGate:
    def test_unrelated_ticker_rejected_when_no_advisor_context(self, client, db, mock_alpaca):
        """Direct API calls without advisor_session_id + rationale still
        hit the whitelist gate. This is the safety path for anything that
        bypasses the normal UI flow."""
        _seed_watchlist(db, ["AAPL", "MSFT"])
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(
                    ticker="ZZZZ",
                    advisor_session_id=None,
                    rationale=None,
                ),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "whitelist" in body["rejection_reason"].lower()

    def test_watchlist_ticker_passes(self, client, db, mock_alpaca):
        _seed_watchlist(db, ["NVDA", "AAPL"])
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("filled", "accepted")

    def test_position_ticker_passes_even_without_watchlist(
        self, client, db, mock_alpaca
    ):
        _seed_watchlist(db, ["AAPL"])  # NVDA not on watchlist
        mock_alpaca.get_positions.return_value = [{"ticker": "NVDA"}]
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 200
        assert resp.json()["status"] in ("filled", "accepted")

    def test_advisor_trade_auto_whitelists_new_ticker(
        self, client, db, mock_alpaca
    ):
        """A trade from the Advisor UI (has advisor_session_id + rationale)
        for a ticker NOT on the watchlist should auto-add it and proceed.
        The whole chain (thesis typed, card clicked, modal confirmed) is
        sufficient deliberate intent."""
        _seed_watchlist(db, ["AAPL"])  # SLV not on watchlist
        mock_alpaca.place_order.return_value = OrderResult(
            order_id="order-slv-1", ticker="SLV", qty=10.0, side="buy",
            status="filled", is_paper=True, fill_price=28.50,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(
                    ticker="SLV",
                    advisor_session_id="advisor-abc123",
                    rationale="Silver breakout — inflation hedge",
                ),
            )
        # Order should succeed, not be rejected
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("filled", "accepted")
        # SLV should now be on the watchlist with auto-added notes
        watchlist_set = crud.get_watchlist_tickers_set(db)
        assert "SLV" in watchlist_set
        rows = crud.list_watchlist_tickers(db)
        slv = next(r for r in rows if r.ticker == "SLV")
        assert "auto-added" in (slv.notes or "").lower()

    def test_advisor_auto_whitelist_needs_both_session_and_rationale(
        self, client, db, mock_alpaca
    ):
        """Auto-whitelist requires BOTH advisor_session_id AND rationale.
        A request with only one (e.g., a half-built direct API call) still
        hits the rejection path — defence against the case where someone
        tries to bypass with a fake session id but forgets the rationale."""
        _seed_watchlist(db, ["AAPL"])
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            # Session without rationale → rejected
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(
                    ticker="ZZZZ",
                    advisor_session_id="advisor-fake",
                    rationale=None,
                ),
            )
        assert resp.json()["status"] == "rejected"
        assert "SLV" not in crud.get_watchlist_tickers_set(db)

        with patch("backend.main.get_broker", return_value=mock_alpaca):
            # Rationale without session → rejected
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(
                    ticker="ZZZZ",
                    advisor_session_id=None,
                    rationale="Some reason",
                ),
            )
        assert resp.json()["status"] == "rejected"


# ── 20% portfolio gate ─────────────────────────────────────────────────────

class TestPortfolioGate:
    def test_oversized_buy_rejected(self, client, mock_alpaca):
        # 100 shares × $450 = $45,000 > 20% of $100k portfolio ($20k)
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(qty=100, limit_price=450.00),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "20%" in body["rejection_reason"]

    def test_within_limit_passes(self, client, mock_alpaca):
        # 10 × $450 = $4,500 < $20k → ok
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 200
        assert resp.json()["status"] in ("filled", "accepted")

    def test_oversized_sell_allowed(self, client, mock_alpaca):
        """The 20% rule is a position-SIZING cap meant to prevent buys
        from blowing up concentration risk. Sells of existing longs
        reduce risk — they must never be rejected by this gate, even
        when the notional is technically > 20% (common for 100% closes
        of big positions)."""
        mock_alpaca.place_order.return_value = OrderResult(
            order_id="sell-big-1", ticker="NVDA", qty=100, side="sell",
            status="filled", is_paper=True, fill_price=460.0,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(
                    side="sell",
                    qty=100,
                    limit_price=450.00,  # $45k notional > $20k cap
                ),
            )
        assert resp.status_code == 200
        body = resp.json()
        # Should NOT be rejected by the portfolio gate
        assert body["status"] in ("filled", "accepted")


# ── Persistence ────────────────────────────────────────────────────────────

class TestPersistence:
    def test_successful_order_persisted(self, client, db, mock_alpaca):
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            client.post("/orders/confirm", json=_valid_request())
        orders = crud.list_executed_orders(db)
        assert len(orders) == 1
        assert orders[0].ticker == "NVDA"
        assert orders[0].alpaca_order_id == "alpaca-order-123"
        assert orders[0].fill_price == 450.12
        assert orders[0].status == "filled"
        assert orders[0].rationale == "Bullish breakout test"
        assert orders[0].advisor_session_id == "advisor-test"

    def test_rejected_order_persisted_with_reason(
        self, client, db, mock_alpaca
    ):
        _seed_watchlist(db, ["AAPL"])  # ZZZZ will be off-whitelist
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            # No advisor context → rejection path stays active
            client.post(
                "/orders/confirm",
                json=_valid_request(
                    ticker="ZZZZ",
                    advisor_session_id=None,
                    rationale=None,
                ),
            )
        orders = crud.list_executed_orders(db)
        assert len(orders) == 1
        assert orders[0].status == "rejected"
        assert orders[0].alpaca_order_id is None
        assert "whitelist" in orders[0].rejection_reason.lower()

    def test_alpaca_failure_persisted(self, client, db, mock_alpaca):
        mock_alpaca.place_order.side_effect = Exception("Alpaca 500: server error")
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            client.post("/orders/confirm", json=_valid_request())
        orders = crud.list_executed_orders(db)
        assert len(orders) == 1
        assert orders[0].status == "failed"
        assert "Alpaca 500" in orders[0].rejection_reason


# ── List endpoint ──────────────────────────────────────────────────────────

class TestListEndpoint:
    def test_list_empty(self, client):
        resp = client.get("/orders/executed")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_filter_by_status(self, client, db):
        crud.create_executed_order(
            db, ticker="NVDA", side="buy", qty=1, order_type="market",
            status="filled", is_paper=True,
        )
        crud.create_executed_order(
            db, ticker="AAPL", side="buy", qty=1, order_type="market",
            status="rejected", is_paper=True, rejection_reason="test",
        )
        resp = client.get("/orders/executed?status=filled")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["ticker"] == "NVDA"

    def test_list_invalid_status(self, client):
        resp = client.get("/orders/executed?status=garbage")
        assert resp.status_code == 422


# ── Advisor prompt addendum flag ───────────────────────────────────────────

class TestAdvisorTradeProposalFlag:
    """The trade-proposal format must only be in the prompt when
    enable_trade_proposals=True. Briefs/reactions must NOT see it."""

    def _rendered_blocks(self, enable_proposals: bool) -> str:
        from backend.agents.trading_advisor_agent import _build_system_blocks
        blocks = _build_system_blocks(
            mode="paper",
            enable_proposals=enable_proposals,
            account={"portfolio_value": 100_000, "buying_power": 200_000, "cash": 100_000},
            positions=[], signals=[], theses=[],
        )
        return "\n".join(b["text"] for b in blocks)

    def test_addendum_present_when_enabled(self):
        rendered = self._rendered_blocks(enable_proposals=True)
        assert "trade-proposal" in rendered
        assert "Trade execution format" in rendered

    def test_addendum_absent_when_disabled(self):
        rendered = self._rendered_blocks(enable_proposals=False)
        assert "trade-proposal" not in rendered
        assert "Trade execution format" not in rendered
        assert "Trade execution format" not in rendered


# ── Scale-out bracket endpoint plumbing ──────────────────────────────────────

class TestScaleOutBracketEndpoint:
    """
    Commit 1 wires target_qty through /orders/confirm so scale-out
    brackets can be specified by the UI. Tests here cover the endpoint-
    level validation and DB persistence. The actual broker-side
    construction lands in the follow-up commit — these tests use a
    mocked broker so we only verify the request-to-DB path.
    """

    def _bracket_request(self, **overrides):
        base = _valid_request(
            ticker="NVDA",
            qty=2,
            stop_loss=440.00,
            target_1=470.00,
        )
        base.update(overrides)
        return base

    def test_scale_out_persists_target_qty_and_fresh_state(self, client, db, mock_alpaca):
        from backend.db.models import ExecutedOrder

        mock_alpaca.place_bracket_order.return_value = OrderResult(
            order_id="ord-123", ticker="NVDA", qty=2.0, side="buy",
            status="accepted", is_paper=True, fill_price=None,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=self._bracket_request(qty=2, target_qty=1),
            )
        assert resp.status_code == 200, resp.text

        row = db.query(ExecutedOrder).first()
        assert row.order_class == "scale_out"
        assert row.target_qty == 1.0
        assert row.bracket_state == "fresh"

    def test_scale_out_forwards_target_qty_to_broker(self, client, mock_alpaca):
        mock_alpaca.place_bracket_order.return_value = OrderResult(
            order_id="ord", ticker="NVDA", qty=4.0, side="buy",
            status="accepted", is_paper=True, fill_price=None,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            client.post(
                "/orders/confirm",
                json=self._bracket_request(qty=4, target_qty=2),
            )
        mock_alpaca.place_bracket_order.assert_called_once()
        kwargs = mock_alpaca.place_bracket_order.call_args.kwargs
        assert kwargs["target_qty"] == 2
        assert kwargs["qty"] == 4

    def test_all_out_bracket_persists_plain_bracket_class(self, client, db, mock_alpaca):
        """Classic bracket (no target_qty) still works — order_class='bracket', bracket_state=null."""
        from backend.db.models import ExecutedOrder

        mock_alpaca.place_bracket_order.return_value = OrderResult(
            order_id="ord", ticker="NVDA", qty=2.0, side="buy",
            status="accepted", is_paper=True, fill_price=None,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=self._bracket_request(qty=2))
        assert resp.status_code == 200, resp.text

        row = db.query(ExecutedOrder).first()
        assert row.order_class == "bracket"
        assert row.target_qty is None
        assert row.bracket_state is None

    def test_target_qty_equal_to_qty_normalises_to_none(self, client, db, mock_alpaca):
        """
        Scale-out with target_qty == qty is a no-op (same as all-out).
        Endpoint normalises the field to None so downstream doesn't have
        to double-check. Row should read like a classic bracket.
        """
        from backend.db.models import ExecutedOrder

        mock_alpaca.place_bracket_order.return_value = OrderResult(
            order_id="ord", ticker="NVDA", qty=2.0, side="buy",
            status="accepted", is_paper=True, fill_price=None,
        )
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            client.post(
                "/orders/confirm",
                json=self._bracket_request(qty=2, target_qty=2),
            )
        row = db.query(ExecutedOrder).first()
        assert row.order_class == "bracket"
        assert row.target_qty is None
        assert row.bracket_state is None

    def test_fractional_target_qty_rejected(self, client, mock_alpaca):
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=self._bracket_request(qty=4, target_qty=1.5),
            )
        assert resp.status_code == 422
        assert "whole number" in resp.json()["detail"].lower()

    def test_fractional_qty_with_scale_out_rejected(self, client, mock_alpaca):
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=self._bracket_request(qty=2.5, target_qty=1),
            )
        assert resp.status_code == 422
        assert "integer qty" in resp.json()["detail"].lower()
