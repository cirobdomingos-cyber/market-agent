"""
Tests for the trade journal feature.

The journal is the discipline layer — every trade gets a user thesis
captured at decision time and a user lesson captured after close. The
critical correctness boundaries:

  1. The /orders/confirm endpoint REQUIRES user_thesis (10+ chars). Without
     it, no order can be placed via the app. This is non-negotiable —
     skipping the thesis defeats the entire purpose.

  2. The polling job creates pending-thesis entries for manual trades,
     and closes existing entries on position_closed events. Both paths
     are best-effort and never block the review generation flow.

  3. The /journal endpoints enforce immutability: thesis can't be
     overwritten once set, lesson can't be overwritten once set, and
     lesson can't be added before the position closes.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.base import AgentResult
from backend.agents.orchestrator import Orchestrator
from backend.brokers import OrderResult
from backend.config import settings
from backend.db import crud, get_db
from backend.db.models import Base
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
    # Seed a default watchlist so trades on common tickers pass the
    # whitelist gate without each test needing to set it up.
    from backend.db.models import WatchlistTicker
    session = _TestSession()
    try:
        for t in ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META", "SPY", "QQQ"]:
            session.add(WatchlistTicker(ticker=t))
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
    mock.place_order.return_value = OrderResult(
        order_id="order-123",
        ticker="NVDA",
        qty=10.0,
        side="buy",
        status="filled",
        is_paper=True,
        fill_price=450.12,
    )
    return mock


def _make_orchestrator(db):
    orch = Orchestrator.__new__(Orchestrator)
    orch.db = db
    orch.client = MagicMock()
    return orch


# ── 1. CRUD ─────────────────────────────────────────────────────────────────

class TestJournalCrud:
    def test_create_and_get(self, db):
        e = crud.create_journal_entry(
            db,
            ticker="NVDA",
            side="buy",
            qty=10,
            open_price=450.0,
            opened_at=datetime.now(timezone.utc),
            user_thesis="My thesis here",
            status="open",
        )
        fetched = crud.get_journal_entry(db, e.id)
        assert fetched.ticker == "NVDA"
        assert fetched.user_thesis == "My thesis here"

    def test_list_filter_by_status(self, db):
        crud.create_journal_entry(
            db, ticker="NVDA", side="buy", qty=10, open_price=450,
            opened_at=datetime.now(timezone.utc), status="open",
        )
        crud.create_journal_entry(
            db, ticker="AAPL", side="buy", qty=20, open_price=180,
            opened_at=datetime.now(timezone.utc), status="closed",
        )
        opens = crud.list_journal_entries(db, status="open")
        assert len(opens) == 1 and opens[0].ticker == "NVDA"
        closed = crud.list_journal_entries(db, status="closed")
        assert len(closed) == 1 and closed[0].ticker == "AAPL"

    def test_get_open_entry_for_ticker_returns_oldest(self, db):
        import time
        e1 = crud.create_journal_entry(
            db, ticker="NVDA", side="buy", qty=5, open_price=440,
            opened_at=datetime.now(timezone.utc), status="open",
        )
        time.sleep(0.01)
        e2 = crud.create_journal_entry(
            db, ticker="NVDA", side="buy", qty=10, open_price=450,
            opened_at=datetime.now(timezone.utc), status="open",
        )
        oldest = crud.get_open_journal_entry_for_ticker(db, "NVDA")
        assert oldest.id == e1.id  # FIFO

    def test_count_action_needed(self, db):
        # Open entry with thesis → no action
        crud.create_journal_entry(
            db, ticker="A", side="buy", qty=1, open_price=10,
            opened_at=datetime.now(timezone.utc),
            user_thesis="have thesis", status="open",
        )
        # Open entry WITHOUT thesis → needs action
        crud.create_journal_entry(
            db, ticker="B", side="buy", qty=1, open_price=10,
            opened_at=datetime.now(timezone.utc), status="open",
        )
        # Closed entry without lesson → needs action
        crud.create_journal_entry(
            db, ticker="C", side="buy", qty=1, open_price=10,
            opened_at=datetime.now(timezone.utc),
            user_thesis="t", close_price=11, status="closed",
        )
        # Closed entry with lesson → no action
        crud.create_journal_entry(
            db, ticker="D", side="buy", qty=1, open_price=10,
            opened_at=datetime.now(timezone.utc),
            user_thesis="t", close_price=11, user_lesson="learned",
            status="closed",
        )
        assert crud.count_journal_entries_needing_action(db) == 2


# ── 2. Order endpoint requires the thesis ───────────────────────────────────

class TestOrderEndpointThesisRequirement:
    def _request(self, **overrides):
        base = {
            "ticker": "NVDA",
            "side": "buy",
            "qty": 10,
            "order_type": "limit",
            "limit_price": 450.0,
            "rationale": "Test rationale",
            "advisor_session_id": "advisor-test",
            "confirm_live_capital": False,
            "user_thesis": "Test thesis with enough characters",
            "user_disagreement": None,
        }
        base.update(overrides)
        return base

    def test_thesis_required(self, client):
        body = self._request()
        del body["user_thesis"]
        resp = client.post("/orders/confirm", json=body)
        assert resp.status_code == 422

    def test_thesis_too_short_rejected(self, client):
        resp = client.post("/orders/confirm", json=self._request(user_thesis="short"))
        assert resp.status_code == 422

    def test_thesis_min_10_chars_accepted(self, client, mock_broker):
        with patch("backend.main.get_broker", return_value=mock_broker):
            resp = client.post(
                "/orders/confirm",
                json=self._request(user_thesis="exactly10c"),  # 10 chars
            )
        assert resp.status_code == 200

    def test_thesis_persisted_to_journal_on_buy(self, client, db, mock_broker):
        with patch("backend.main.get_broker", return_value=mock_broker):
            client.post("/orders/confirm", json=self._request())
        entries = crud.list_journal_entries(db)
        assert len(entries) == 1
        assert entries[0].ticker == "NVDA"
        assert entries[0].user_thesis == "Test thesis with enough characters"
        assert entries[0].advisor_rationale == "Test rationale"
        assert entries[0].advisor_session_id == "advisor-test"
        assert entries[0].status == "open"

    def test_sell_does_not_create_journal_entry(self, client, db, mock_broker):
        """Sells are usually closes — the polling job handles them via the
        position-closed detection path, not via /orders/confirm."""
        # Mock the broker's place_order to return a sell result
        mock_broker.place_order.return_value = OrderResult(
            order_id="sell-1", ticker="NVDA", qty=10, side="sell",
            status="filled", is_paper=True, fill_price=460.0,
        )
        with patch("backend.main.get_broker", return_value=mock_broker):
            client.post("/orders/confirm", json=self._request(side="sell"))
        # Order was recorded
        assert len(crud.list_executed_orders(db)) == 1
        # But no journal entry created
        assert crud.list_journal_entries(db) == []


# ── 3. Polling integration — manual trades + closes ─────────────────────────

class TestPollingJournalIntegration:
    def setup_method(self):
        self._patches = [
            patch.object(settings, "position_reviews_enabled", True),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def _orch_with_broker(self, db, positions):
        orch = _make_orchestrator(db)
        mock = MagicMock()
        mock.get_positions.return_value = positions
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        return orch, mock

    def test_manual_trade_creates_pending_thesis_entry(self, db):
        """A position appears with no matching open journal entry → it was
        a manual trade. Create a journal entry with no thesis so the user
        can fill it in later."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock = MagicMock()
        mock.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock):
            orch._check_position_changes()  # baseline (empty)
            mock.get_positions.return_value = [{
                "ticker": "TSLA",
                "qty": 5,
                "avg_entry": 200.0,
                "current_price": 210.0,
                "unrealised_pnl_pct": 5.0,
            }]
            orch._check_position_changes()

        entries = crud.list_journal_entries(db)
        assert len(entries) == 1
        assert entries[0].ticker == "TSLA"
        assert entries[0].user_thesis is None  # needs_thesis=True in serialiser
        assert entries[0].status == "open"

    def test_existing_journal_entry_not_duplicated_on_open_event(self, db):
        """If a journal entry already exists for the ticker (e.g. created
        via /orders/confirm), the polling job MUST NOT create a second one
        when the position appears in the next poll."""
        # Pre-create an entry as if the user confirmed via the app
        crud.create_journal_entry(
            db, ticker="NVDA", side="buy", qty=10, open_price=450,
            opened_at=datetime.now(timezone.utc),
            user_thesis="My pre-existing thesis", status="open",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock = MagicMock()
        mock.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock):
            orch._check_position_changes()  # baseline (empty)
            mock.get_positions.return_value = [{
                "ticker": "NVDA",
                "qty": 10,
                "avg_entry": 450.0,
                "current_price": 460.0,
                "unrealised_pnl_pct": 2.2,
            }]
            orch._check_position_changes()

        # Still only the one journal entry — the polling job did not duplicate
        entries = crud.list_journal_entries(db)
        assert len(entries) == 1
        assert entries[0].user_thesis == "My pre-existing thesis"

    def test_position_closed_fills_in_close_fields(self, db):
        """When the polling job detects a close, it finds the open journal
        entry and fills in close_price/closed_at/pnl/status=closed."""
        # Pre-create an open entry
        crud.create_journal_entry(
            db, ticker="NVDA", side="buy", qty=10, open_price=450,
            opened_at=datetime.now(timezone.utc) - timedelta(days=2),
            user_thesis="My thesis", status="open",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock = MagicMock()
        # Baseline: position is held
        mock.get_positions.return_value = [{
            "ticker": "NVDA",
            "qty": 10,
            "avg_entry": 450.0,
            "current_price": 470.0,
            "unrealised_pnl_pct": 4.4,
        }]
        with patch("backend.agents.orchestrator.get_broker", return_value=mock):
            orch._check_position_changes()  # baseline run
            # Next poll: position is gone
            mock.get_positions.return_value = []
            orch._check_position_changes()

        entry = crud.list_journal_entries(db)[0]
        assert entry.status == "closed"
        assert entry.close_price == 470.0  # last seen current_price
        assert entry.pnl_pct == pytest.approx((470 - 450) / 450 * 100, rel=1e-3)
        assert entry.closed_at is not None
        assert entry.advised_direction_profitable is True


# ── 4. /journal API endpoints ───────────────────────────────────────────────

class TestJournalEndpoints:
    def _seed(self, db, **overrides):
        base = dict(
            ticker="NVDA", side="buy", qty=10, open_price=450,
            opened_at=datetime.now(timezone.utc),
            user_thesis="Test thesis here", status="open",
        )
        base.update(overrides)
        return crud.create_journal_entry(db, **base)

    def test_list_empty(self, client):
        resp = client.get("/journal")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_entries(self, client, db):
        self._seed(db)
        resp = client.get("/journal")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["ticker"] == "NVDA"
        assert body[0]["needs_thesis"] is False
        assert body[0]["needs_action"] is False

    def test_list_invalid_status(self, client):
        resp = client.get("/journal?status=garbage")
        assert resp.status_code == 422

    def test_action_needed_count_endpoint(self, client, db):
        # Create one needs-thesis and one needs-lesson entry
        self._seed(db, ticker="A", user_thesis=None)
        self._seed(db, ticker="B", status="closed", user_lesson=None)
        resp = client.get("/journal/action-needed-count")
        assert resp.json() == {"count": 2}

    def test_get_entry_by_id(self, client, db):
        e = self._seed(db)
        resp = client.get(f"/journal/{e.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == e.id

    def test_get_entry_404(self, client):
        resp = client.get("/journal/missing-id")
        assert resp.status_code == 404

    def test_add_thesis_to_pending_entry(self, client, db):
        e = self._seed(db, user_thesis=None)  # pending thesis (manual trade)
        resp = client.post(
            f"/journal/{e.id}/thesis",
            json={
                "user_thesis": "Backfilled thesis after the fact",
                "user_disagreement": None,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user_thesis"] == "Backfilled thesis after the fact"
        assert resp.json()["needs_thesis"] is False

    def test_thesis_immutable_once_set(self, client, db):
        e = self._seed(db, user_thesis="original thesis")
        resp = client.post(
            f"/journal/{e.id}/thesis",
            json={"user_thesis": "trying to overwrite the thesis"},
        )
        assert resp.status_code == 409

    def test_add_lesson_after_close(self, client, db):
        e = self._seed(db, status="closed", close_price=460, pnl_pct=2.2)
        resp = client.post(
            f"/journal/{e.id}/lesson",
            json={"user_lesson": "Learned something useful here"},
        )
        assert resp.status_code == 200
        assert resp.json()["needs_lesson"] is False

    def test_lesson_rejected_while_open(self, client, db):
        e = self._seed(db, status="open")
        resp = client.post(
            f"/journal/{e.id}/lesson",
            json={"user_lesson": "Trying to add a lesson early"},
        )
        assert resp.status_code == 409

    def test_lesson_immutable_once_set(self, client, db):
        e = self._seed(
            db, status="closed", close_price=460,
            user_lesson="already saved lesson",
        )
        resp = client.post(
            f"/journal/{e.id}/lesson",
            json={"user_lesson": "trying to overwrite the lesson"},
        )
        assert resp.status_code == 409

    def test_thesis_too_short_rejected_by_endpoint(self, client, db):
        e = self._seed(db, user_thesis=None)
        resp = client.post(
            f"/journal/{e.id}/thesis",
            json={"user_thesis": "short"},
        )
        assert resp.status_code == 422

    def test_lesson_too_short_rejected_by_endpoint(self, client, db):
        e = self._seed(db, status="closed", close_price=460)
        resp = client.post(
            f"/journal/{e.id}/lesson",
            json={"user_lesson": "short"},
        )
        assert resp.status_code == 422
