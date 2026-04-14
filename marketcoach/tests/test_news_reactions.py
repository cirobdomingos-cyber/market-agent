"""
Tests for the auto news-reactions feature.

Covers three layers:
  1. CRUD — create, list, status updates, dedupe-window query, unread count
  2. Filter logic — _trigger_news_reactions correctly classifies, dedupes,
     prioritises, and rate-limits without making real Anthropic calls
  3. API — list / unread-count / read / dismiss / 404 / status validation

The orchestrator's run_advisor is patched in the filter tests so we never
hit the real Anthropic API. We assert on what the filter *decides* to call,
not on what the model returns.
"""

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.base import AgentResult
from backend.agents.orchestrator import Orchestrator
from backend.config import settings
from backend.db import crud, get_db
from backend.db.models import Base
from backend.main import app


# ── Shared in-memory DB setup ───────────────────────────────────────────────

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


# ── 1. CRUD layer ───────────────────────────────────────────────────────────

class TestReactionCrud:
    def test_create_and_list(self, db):
        crud.create_news_reaction(
            db,
            ticker="NVDA",
            headline="Nvidia beats earnings",
            content="bullish",
            trigger_reason="position",
        )
        rs = crud.list_news_reactions(db)
        assert len(rs) == 1
        assert rs[0].ticker == "NVDA"
        assert rs[0].status == "unread"  # default

    def test_unread_count(self, db):
        crud.create_news_reaction(
            db, ticker="A", headline="x", content="y", trigger_reason="watchlist"
        )
        crud.create_news_reaction(
            db, ticker="B", headline="x", content="y", trigger_reason="watchlist",
            status="read",
        )
        crud.create_news_reaction(
            db, ticker="C", headline="x", content="y", trigger_reason="watchlist",
            status="dismissed",
        )
        assert crud.count_unread_reactions(db) == 1

    def test_status_update(self, db):
        r = crud.create_news_reaction(
            db, ticker="A", headline="x", content="y", trigger_reason="watchlist"
        )
        updated = crud.update_reaction_status(db, r.id, "read")
        assert updated.status == "read"
        assert crud.update_reaction_status(db, "missing-id", "read") is None

    def test_filter_by_status(self, db):
        crud.create_news_reaction(
            db, ticker="A", headline="x", content="y", trigger_reason="watchlist"
        )
        crud.create_news_reaction(
            db, ticker="B", headline="x", content="y", trigger_reason="watchlist",
            status="dismissed",
        )
        unread = crud.list_news_reactions(db, status="unread")
        assert len(unread) == 1 and unread[0].ticker == "A"

        dismissed = crud.list_news_reactions(db, status="dismissed")
        assert len(dismissed) == 1 and dismissed[0].ticker == "B"

    def test_dedupe_window(self, db):
        crud.create_news_reaction(
            db, ticker="NVDA", headline="x", content="y", trigger_reason="position"
        )
        # Within window → should be detected
        assert crud.has_recent_reaction_for_ticker(db, "NVDA", hours=24) is True
        # Different ticker → no hit
        assert crud.has_recent_reaction_for_ticker(db, "AAPL", hours=24) is False


# ── 2. Filter / orchestrator logic ──────────────────────────────────────────

def _make_orchestrator(db):
    """Build an Orchestrator with a mocked Anthropic client (never called)."""
    orch = Orchestrator.__new__(Orchestrator)  # bypass __init__ to skip API key
    orch.db = db
    orch.client = MagicMock()
    return orch


class TestReactionFilter:
    def setup_method(self):
        # Reset settings to deterministic defaults for each test
        self._patches = [
            patch.object(settings, "news_reactions_enabled", True),
            patch.object(settings, "news_reaction_min_confidence", 0.8),
            patch.object(settings, "news_reactions_per_run_max", 5),
            patch.object(settings, "news_reaction_dedupe_hours", 6),
            patch.object(settings, "default_watchlist", "NVDA,AAPL,MSFT,SPY"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def _signal(self, ticker, confidence=0.9, sentiment="bullish", headline="news"):
        return {
            "ticker": ticker,
            "confidence": confidence,
            "sentiment": sentiment,
            "headline": headline,
        }

    def test_disabled_returns_zero(self, db):
        with patch.object(settings, "news_reactions_enabled", False):
            orch = _make_orchestrator(db)
            assert orch._trigger_news_reactions([self._signal("NVDA")]) == 0

    def test_low_confidence_filtered_out(self, db):
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("NVDA", confidence=0.5),
            ])
        assert n == 0
        assert crud.list_news_reactions(db) == []

    def test_neutral_sentiment_filtered_out(self, db):
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("NVDA", sentiment="neutral"),
            ])
        assert n == 0

    def test_unrelated_ticker_filtered_out(self, db):
        """Tickers not in positions/theses/watchlist must be skipped."""
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("RANDOM_NOT_IN_LIST"),
            ])
        assert n == 0

    def test_watchlist_match_creates_reaction(self, db):
        orch = _make_orchestrator(db)
        # Mock run_advisor so we don't hit the real API
        orch.run_advisor = MagicMock(
            return_value=AgentResult(
                success=True, data={"reply": "advisor markdown"}
            )
        )
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([self._signal("NVDA")])
        assert n == 1
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].ticker == "NVDA"
        assert reactions[0].trigger_reason == "watchlist"
        assert reactions[0].content == "advisor markdown"
        assert reactions[0].status == "unread"

    def test_position_priority_over_watchlist(self, db):
        """When per_run_max=1 and we have one position + one watchlist signal,
        the position one must win."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "x"})
        )
        # Mock alpaca to return AAPL as a position
        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = [{"ticker": "AAPL"}]

        with patch.object(settings, "news_reactions_per_run_max", 1), \
             patch("backend.agents.orchestrator.get_alpaca_client", return_value=mock_alpaca):
            n = orch._trigger_news_reactions([
                self._signal("NVDA"),  # watchlist only
                self._signal("AAPL"),  # position
            ])

        assert n == 1
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].ticker == "AAPL"
        assert reactions[0].trigger_reason == "position"

    def test_dedupe_skips_recent_ticker(self, db):
        """A signal for a ticker that already has a recent reaction is skipped."""
        crud.create_news_reaction(
            db,
            ticker="NVDA",
            headline="prior",
            content="prior",
            trigger_reason="watchlist",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "x"})
        )
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([self._signal("NVDA")])
        assert n == 0
        # Still only the original reaction
        assert len(crud.list_news_reactions(db)) == 1

    def test_per_run_cap_enforced(self, db):
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "x"})
        )
        with patch.object(settings, "news_reactions_per_run_max", 2), \
             patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("NVDA"),
                self._signal("AAPL"),
                self._signal("MSFT"),
                self._signal("SPY"),
            ])
        assert n == 2
        assert len(crud.list_news_reactions(db)) == 2

    def test_advisor_failure_persisted_as_failed(self, db):
        """If run_advisor returns success=False, the reaction is still recorded
        with status='failed' so the user can see the error in the UI."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(
                success=False, data={"reply": ""}, error="rate limit"
            )
        )
        with patch("backend.agents.orchestrator.get_alpaca_client", return_value=None):
            n = orch._trigger_news_reactions([self._signal("NVDA")])
        assert n == 1
        r = crud.list_news_reactions(db)[0]
        assert r.status == "failed"
        assert r.error == "rate limit"


# ── 3. API layer ────────────────────────────────────────────────────────────

class TestReactionApi:
    def test_list_empty(self, client):
        resp = client.get("/news-reactions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_unread_count_empty(self, client):
        resp = client.get("/news-reactions/unread-count")
        assert resp.status_code == 200
        assert resp.json() == {"unread": 0}

    def test_list_returns_serialised_reactions(self, client, db):
        crud.create_news_reaction(
            db,
            ticker="NVDA",
            headline="Nvidia ships new chip",
            content="# bullish",
            trigger_reason="position",
        )
        resp = client.get("/news-reactions")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["ticker"] == "NVDA"
        assert body[0]["trigger_reason"] == "position"
        assert body[0]["status"] == "unread"
        assert "created_at" in body[0]

    def test_list_status_filter(self, client, db):
        crud.create_news_reaction(
            db, ticker="A", headline="x", content="y", trigger_reason="watchlist"
        )
        crud.create_news_reaction(
            db, ticker="B", headline="x", content="y", trigger_reason="watchlist",
            status="dismissed",
        )

        resp = client.get("/news-reactions?status=unread")
        assert len(resp.json()) == 1
        assert resp.json()[0]["ticker"] == "A"

        resp = client.get("/news-reactions?status=dismissed")
        assert len(resp.json()) == 1
        assert resp.json()[0]["ticker"] == "B"

    def test_invalid_status_filter_rejected(self, client):
        resp = client.get("/news-reactions?status=garbage")
        assert resp.status_code == 422

    def test_mark_read(self, client, db):
        r = crud.create_news_reaction(
            db, ticker="NVDA", headline="x", content="y", trigger_reason="position"
        )
        resp = client.post(f"/news-reactions/{r.id}/read")
        assert resp.status_code == 200
        assert resp.json()["status"] == "read"
        assert crud.count_unread_reactions(db) == 0

    def test_dismiss(self, client, db):
        r = crud.create_news_reaction(
            db, ticker="NVDA", headline="x", content="y", trigger_reason="position"
        )
        resp = client.post(f"/news-reactions/{r.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

    def test_mark_read_404(self, client):
        resp = client.post("/news-reactions/missing-id/read")
        assert resp.status_code == 404

    def test_unread_count_reflects_state(self, client, db):
        crud.create_news_reaction(
            db, ticker="A", headline="x", content="y", trigger_reason="watchlist"
        )
        crud.create_news_reaction(
            db, ticker="B", headline="x", content="y", trigger_reason="watchlist"
        )
        assert client.get("/news-reactions/unread-count").json() == {"unread": 2}

        # Mark one read → count drops
        rs = crud.list_news_reactions(db)
        client.post(f"/news-reactions/{rs[0].id}/read")
        assert client.get("/news-reactions/unread-count").json() == {"unread": 1}
