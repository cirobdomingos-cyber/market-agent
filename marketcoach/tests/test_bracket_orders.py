"""
Tests for bracket-order routing through /orders/confirm.

A bracket order is the fix for "manual exit discipline" — when the advisor
emits stop_loss + target_1, the broker wires the parent entry plus a
take-profit limit plus a stop-loss stop as a single OCO group. When one
exit fills, the other cancels at the broker. No polling, no drift.

These tests cover:
  1. Schema gate — both bracket levels required, levels must be consistent
  2. Routing — stop_loss+target_1 set ⇒ place_bracket_order called with the
     right args; otherwise the legacy place_order path is used
  3. Persistence — order_class/stop_loss_price/take_profit_price round-trip
     through the executed_orders audit trail
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.brokers import OrderResult
from backend.db import crud, get_db
from backend.db.models import Base, WatchlistTicker
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
    session = _TestSession()
    try:
        session.add(WatchlistTicker(ticker="NVDA"))
        session.commit()
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
def mock_broker():
    mock = MagicMock()
    mock.get_positions.return_value = []
    mock.get_account.return_value = {
        "equity": 100_000.0,
        "buying_power": 200_000.0,
        "cash": 100_000.0,
        "portfolio_value": 100_000.0,
        "paper": True,
    }
    # Simple path — used for non-bracket submissions.
    mock.place_order.return_value = OrderResult(
        order_id="plain-1",
        ticker="NVDA",
        qty=10.0,
        side="buy",
        status="filled",
        is_paper=True,
        fill_price=450.0,
    )
    # Bracket path — parent accepted but not yet filled (typical for GTC
    # limit entries submitted outside RTH).
    mock.place_bracket_order.return_value = OrderResult(
        order_id="bracket-1",
        ticker="NVDA",
        qty=10.0,
        side="buy",
        status="accepted",
        is_paper=True,
        fill_price=None,
    )
    return mock


def _valid_bracket_request(**overrides):
    """Build a valid BUY + LIMIT + BRACKET request body.

    Defaults satisfy stop_loss < limit_price < target_1. Override
    individual keys to test gate rejections.
    """
    base = {
        "ticker": "NVDA",
        "side": "buy",
        "qty": 10,
        "order_type": "limit",
        "limit_price": 450.0,
        "stop_loss": 440.0,
        "target_1": 470.0,
        "rationale": "Bracket breakout test",
        "advisor_session_id": "advisor-test",
        "confirm_live_capital": False,
        "user_thesis": "Test thesis — NVDA breakout with tight stop",
        "user_disagreement": None,
    }
    base.update(overrides)
    return base


# ── 1. Schema / gate validation ────────────────────────────────────────────

class TestBracketGate:
    def test_partial_bracket_stop_only_rejected(self, client, mock_broker):
        # Only stop_loss, no target_1 → 422. "Both or neither" rule.
        req = _valid_bracket_request(target_1=None)
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422
        assert "both" in resp.json()["detail"].lower()
        mock_broker.place_bracket_order.assert_not_called()
        mock_broker.place_order.assert_not_called()

    def test_partial_bracket_target_only_rejected(self, client, mock_broker):
        req = _valid_bracket_request(stop_loss=None)
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422

    def test_sell_bracket_rejected(self, client, mock_broker):
        # v1: brackets are buy-only. Shorts would need reversed semantics.
        req = _valid_bracket_request(side="sell")
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422
        assert "buy" in resp.json()["detail"].lower()

    def test_market_bracket_rejected(self, client, mock_broker):
        # Market parent doesn't give us a defined entry reference to
        # bound the stop/target against.
        req = _valid_bracket_request(order_type="market", limit_price=None)
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422
        assert "limit" in resp.json()["detail"].lower()

    def test_stop_above_limit_rejected(self, client, mock_broker):
        # stop_loss must be strictly less than limit_price for a long bracket.
        req = _valid_bracket_request(stop_loss=455.0)  # > 450 limit
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422
        assert "inconsistent" in resp.json()["detail"].lower()

    def test_target_below_limit_rejected(self, client, mock_broker):
        req = _valid_bracket_request(target_1=445.0)  # < 450 limit
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 422
        assert "inconsistent" in resp.json()["detail"].lower()


# ── 2. Routing ──────────────────────────────────────────────────────────────

class TestBracketRouting:
    def test_bracket_request_routes_to_place_bracket_order(
        self, client, mock_broker
    ):
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post(
                "/orders/confirm", json=_valid_bracket_request()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["order_class"] == "bracket"
        assert body["stop_loss_price"] == 440.0
        assert body["take_profit_price"] == 470.0
        # The bracket path was used — not the legacy single-order path.
        mock_broker.place_bracket_order.assert_called_once()
        mock_broker.place_order.assert_not_called()

        kwargs = mock_broker.place_bracket_order.call_args.kwargs
        assert kwargs["ticker"] == "NVDA"
        assert kwargs["side"] == "buy"
        assert kwargs["qty"] == 10
        assert kwargs["limit_price"] == 450.0
        assert kwargs["stop_loss_price"] == 440.0
        assert kwargs["take_profit_price"] == 470.0

    def test_non_bracket_request_still_uses_place_order(
        self, client, mock_broker
    ):
        # Same valid request WITHOUT bracket fields — should take the old
        # path to preserve backwards compat for users who don't set exits.
        req = _valid_bracket_request(stop_loss=None, target_1=None)
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=req)
        assert resp.status_code == 200
        body = resp.json()
        assert body["order_class"] == "simple"
        assert body["stop_loss_price"] is None
        assert body["take_profit_price"] is None
        mock_broker.place_order.assert_called_once()
        mock_broker.place_bracket_order.assert_not_called()

    def test_bracket_broker_error_persists_failed_row(
        self, client, db, mock_broker
    ):
        # Broker raises → we still want an audit trail with order_class=bracket
        # and the attempted levels captured on the row.
        mock_broker.place_bracket_order.side_effect = RuntimeError(
            "simulated broker outage"
        )
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post(
                "/orders/confirm", json=_valid_bracket_request()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["order_class"] == "bracket"
        assert body["stop_loss_price"] == 440.0
        assert body["take_profit_price"] == 470.0
        assert "outage" in body["rejection_reason"]


# ── 3. Persistence audit trail ──────────────────────────────────────────────

class TestBracketPersistence:
    def test_executed_order_columns_written(self, client, db, mock_broker):
        with patch("backend.main.get_broker", return_value=mock_broker):
            client.post("/orders/confirm", json=_valid_bracket_request())
        rows = crud.list_executed_orders(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.order_class == "bracket"
        assert row.stop_loss_price == 440.0
        assert row.take_profit_price == 470.0
        assert row.order_type == "limit"
        assert row.limit_price == 450.0

    def test_legacy_order_reads_back_as_simple(self, client, db, mock_broker):
        req = _valid_bracket_request(stop_loss=None, target_1=None)
        with patch("backend.main.get_broker", return_value=mock_broker):
            client.post("/orders/confirm", json=req)
        rows = crud.list_executed_orders(db)
        assert len(rows) == 1
        # order_class defaults to "simple" on serialise even if DB stored
        # something (null gets coerced for forward compat).
        row = rows[0]
        assert row.order_class == "simple"
        assert row.stop_loss_price is None
        assert row.take_profit_price is None
