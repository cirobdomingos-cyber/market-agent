"""
Tests for the watchlist feature.

The watchlist replaces the old DEFAULT_WATCHLIST env var as the source of
truth at runtime. Tests cover:

  1. CRUD — add/list/remove/idempotent-upsert/seed-if-empty
  2. API — GET/POST/DELETE endpoints + validation
  3. Integration — the /orders/confirm whitelist gate reads from the DB
     (not settings.default_watchlist) so adding a ticker via POST /watchlist
     immediately unblocks trades for it, no restart required
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
    mock.place_order.return_value = OrderResult(
        order_id="order-1",
        ticker="GLD",
        qty=10.0,
        side="buy",
        status="filled",
        is_paper=True,
        fill_price=180.50,
    )
    return mock


# ── 1. CRUD ─────────────────────────────────────────────────────────────────

class TestWatchlistCrud:
    def test_empty_state(self, db):
        assert crud.list_watchlist_tickers(db) == []
        assert crud.get_watchlist_tickers_set(db) == set()

    def test_add_and_list(self, db):
        crud.add_watchlist_ticker(db, "nvda", notes="AI bellwether")
        crud.add_watchlist_ticker(db, "AAPL")
        rows = crud.list_watchlist_tickers(db)
        tickers = [r.ticker for r in rows]
        # Alphabetical order + uppercased
        assert tickers == ["AAPL", "NVDA"]
        assert rows[1].notes == "AI bellwether"

    def test_add_is_idempotent(self, db):
        crud.add_watchlist_ticker(db, "NVDA", notes="first")
        crud.add_watchlist_ticker(db, "NVDA", notes="updated")
        rows = crud.list_watchlist_tickers(db)
        assert len(rows) == 1
        assert rows[0].notes == "updated"

    def test_add_without_notes_preserves_existing(self, db):
        crud.add_watchlist_ticker(db, "NVDA", notes="original")
        crud.add_watchlist_ticker(db, "NVDA")  # no notes arg
        rows = crud.list_watchlist_tickers(db)
        assert rows[0].notes == "original"

    def test_remove(self, db):
        crud.add_watchlist_ticker(db, "NVDA")
        crud.add_watchlist_ticker(db, "AAPL")
        assert crud.remove_watchlist_ticker(db, "nvda") is True
        tickers = [r.ticker for r in crud.list_watchlist_tickers(db)]
        assert tickers == ["AAPL"]

    def test_remove_nonexistent_returns_false(self, db):
        assert crud.remove_watchlist_ticker(db, "GLD") is False

    def test_get_as_set(self, db):
        crud.add_watchlist_ticker(db, "NVDA")
        crud.add_watchlist_ticker(db, "AAPL")
        assert crud.get_watchlist_tickers_set(db) == {"NVDA", "AAPL"}

    def test_seed_if_empty_populates(self, db):
        count = crud.seed_watchlist_if_empty(db, ["NVDA", "AAPL", "MSFT"])
        assert count == 3
        assert crud.get_watchlist_tickers_set(db) == {"NVDA", "AAPL", "MSFT"}

    def test_seed_if_empty_is_noop_when_populated(self, db):
        crud.add_watchlist_ticker(db, "GLD")
        count = crud.seed_watchlist_if_empty(db, ["NVDA", "AAPL"])
        assert count == 0
        # GLD is still the only thing there — NVDA and AAPL not added
        assert crud.get_watchlist_tickers_set(db) == {"GLD"}


# ── 2. API endpoints ────────────────────────────────────────────────────────

class TestWatchlistApi:
    def test_get_empty(self, client):
        resp = client.get("/watchlist")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_returns_all_tickers(self, client, db):
        db.add(WatchlistTicker(ticker="NVDA", notes="AI"))
        db.add(WatchlistTicker(ticker="AAPL"))
        db.commit()
        resp = client.get("/watchlist")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["ticker"] == "AAPL"  # alpha order
        assert body[1]["ticker"] == "NVDA"
        assert body[1]["notes"] == "AI"

    def test_post_adds_ticker(self, client, db):
        resp = client.post("/watchlist", json={"ticker": "GLD", "notes": "Gold hedge"})
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "GLD"
        assert crud.get_watchlist_tickers_set(db) == {"GLD"}

    def test_post_uppercases_ticker(self, client):
        resp = client.post("/watchlist", json={"ticker": "gld"})
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "GLD"

    def test_post_rejects_invalid_ticker(self, client):
        # Too long
        resp = client.post("/watchlist", json={"ticker": "TOOLONG"})
        assert resp.status_code == 422
        # Empty
        resp = client.post("/watchlist", json={"ticker": ""})
        assert resp.status_code == 422
        # Non-alpha
        resp = client.post("/watchlist", json={"ticker": "AB12"})
        assert resp.status_code == 422

    def test_delete_removes_ticker(self, client, db):
        db.add(WatchlistTicker(ticker="NVDA"))
        db.commit()
        resp = client.delete("/watchlist/NVDA")
        assert resp.status_code == 200
        assert crud.get_watchlist_tickers_set(db) == set()

    def test_delete_404_when_missing(self, client):
        resp = client.delete("/watchlist/ZZZZ")
        assert resp.status_code == 404


# ── 3. Integration with /orders/confirm whitelist gate ─────────────────────

class TestWatchlistUnblocksOrders:
    """The whole point of the feature: adding a ticker to the watchlist via
    the POST /watchlist endpoint must immediately allow /orders/confirm to
    accept trades for that ticker, with no restart."""

    def _valid_request(self, ticker="GLD"):
        return {
            "ticker": ticker,
            "side": "buy",
            "qty": 10,
            "order_type": "market",
            "limit_price": None,
            "rationale": "Test",
            "advisor_session_id": "test",
            "confirm_live_capital": False,
            "user_thesis": "Test thesis covering at least 10 chars",
        }

    def test_gld_rejected_when_not_on_watchlist(self, client, mock_broker):
        # Empty watchlist — GLD should be rejected
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=self._valid_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "whitelist" in body["rejection_reason"].lower()

    def test_gld_accepted_after_adding_to_watchlist(self, client, mock_broker):
        # Add GLD via the API — the next order for GLD should go through
        client.post("/watchlist", json={"ticker": "GLD"})
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=self._valid_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("filled", "accepted")

    def test_removing_from_watchlist_re_rejects(self, client, db, mock_broker):
        client.post("/watchlist", json={"ticker": "GLD"})
        client.delete("/watchlist/GLD")
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post("/orders/confirm", json=self._valid_request())
        assert resp.json()["status"] == "rejected"
