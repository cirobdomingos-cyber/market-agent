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
    def test_unrelated_ticker_rejected(self, client, mock_alpaca, monkeypatch):
        monkeypatch.setattr(settings, "default_watchlist", "AAPL,MSFT")
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post(
                "/orders/confirm",
                json=_valid_request(ticker="ZZZZ"),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "whitelist" in body["rejection_reason"].lower()

    def test_watchlist_ticker_passes(self, client, mock_alpaca, monkeypatch):
        monkeypatch.setattr(settings, "default_watchlist", "NVDA,AAPL")
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("filled", "accepted")

    def test_position_ticker_passes_even_without_watchlist(
        self, client, mock_alpaca, monkeypatch
    ):
        monkeypatch.setattr(settings, "default_watchlist", "AAPL")
        mock_alpaca.get_positions.return_value = [{"ticker": "NVDA"}]
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            resp = client.post("/orders/confirm", json=_valid_request())
        assert resp.status_code == 200
        assert resp.json()["status"] in ("filled", "accepted")


# ── 20% portfolio gate ─────────────────────────────────────────────────────

class TestPortfolioGate:
    def test_oversized_order_rejected(self, client, mock_alpaca):
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
        self, client, db, mock_alpaca, monkeypatch
    ):
        monkeypatch.setattr(settings, "default_watchlist", "AAPL")
        with patch("backend.main.get_broker", return_value=mock_alpaca):
            client.post("/orders/confirm", json=_valid_request(ticker="ZZZZ"))
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
