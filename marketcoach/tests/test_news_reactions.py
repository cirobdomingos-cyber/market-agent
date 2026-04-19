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
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    @pytest.fixture(autouse=True)
    def _seed_watchlist_for_tests(self, db):
        """Seed a deterministic watchlist in the DB for every test in this
        class. Migrated from monkeypatching settings.default_watchlist —
        the runtime now reads the watchlist from the DB, not env."""
        from backend.db.models import WatchlistTicker
        for t in ["NVDA", "AAPL", "MSFT", "SPY"]:
            db.add(WatchlistTicker(ticker=t))
        db.commit()

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
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("NVDA", confidence=0.5),
            ])
        assert n == 0
        assert crud.list_news_reactions(db) == []

    def test_neutral_sentiment_filtered_out(self, db):
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
            n = orch._trigger_news_reactions([
                self._signal("NVDA", sentiment="neutral"),
            ])
        assert n == 0

    def test_unrelated_ticker_filtered_out(self, db):
        """Tickers not in positions/theses/watchlist must be skipped."""
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
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
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
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
             patch("backend.agents.orchestrator.get_broker", return_value=mock_alpaca):
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
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
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
             patch("backend.agents.orchestrator.get_broker", return_value=None):
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
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
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


# ── 4. State-aware advisor gate (material-change rule) ──────────────────────

class TestMaterialChangeHelpers:
    """
    Unit tests for the module-level helpers that decide whether a news
    reaction should skip the advisor call. Pure functions, no DB or API —
    just logic that must be right because it gates real token spend.
    """

    def test_should_skip_no_cache_runs_advisor(self):
        from backend.agents.orchestrator import _should_skip_advisor
        from datetime import datetime, timezone
        now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        skip, reason = _should_skip_advisor(None, 100.0, now)
        assert skip is False
        assert "no-prior" in reason

    def test_should_skip_within_thresholds(self):
        from backend.agents.orchestrator import _should_skip_advisor
        from datetime import datetime, timezone, timedelta
        from backend.db.models import AdvisorActionCache
        now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        cached = AdvisorActionCache(
            ticker="NVDA",
            last_run_at=now - timedelta(hours=2),
            last_price=450.0,
            last_recommendation="hold",
        )
        # Price moved 1.1%, 2h elapsed — both under thresholds
        skip, reason = _should_skip_advisor(cached, 455.0, now)
        assert skip is True
        assert "fresh" in reason
        assert "hold" in reason  # prior recommendation surfaced

    def test_should_not_skip_when_price_moves_3pct(self):
        from backend.agents.orchestrator import _should_skip_advisor
        from datetime import datetime, timezone, timedelta
        from backend.db.models import AdvisorActionCache
        now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        cached = AdvisorActionCache(
            ticker="NVDA",
            last_run_at=now - timedelta(hours=2),
            last_price=450.0,
            last_recommendation="hold",
        )
        # Moved ~3.3% down — crosses the threshold
        skip, reason = _should_skip_advisor(cached, 435.0, now)
        assert skip is False
        assert "price moved" in reason

    def test_should_not_skip_after_48h(self):
        from backend.agents.orchestrator import _should_skip_advisor
        from datetime import datetime, timezone, timedelta
        from backend.db.models import AdvisorActionCache
        now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        cached = AdvisorActionCache(
            ticker="NVDA",
            last_run_at=now - timedelta(hours=49),
            last_price=450.0,
            last_recommendation="hold",
        )
        # Price stable but time threshold crossed
        skip, reason = _should_skip_advisor(cached, 451.0, now)
        assert skip is False
        assert "stale" in reason

    def test_should_not_skip_when_cached_price_missing(self):
        from backend.agents.orchestrator import _should_skip_advisor
        from datetime import datetime, timezone, timedelta
        from backend.db.models import AdvisorActionCache
        now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        cached = AdvisorActionCache(
            ticker="NVDA",
            last_run_at=now - timedelta(hours=2),
            last_price=0.0,  # pathological
        )
        skip, reason = _should_skip_advisor(cached, 100.0, now)
        assert skip is False
        assert "no-prior-price" in reason


class TestExtractRecommendation:
    """
    Heuristic extraction from free-form advisor text. Keyword-based — not
    perfect, but good enough for cache rollups that don't need to be exact.
    """

    def test_buy_detected(self):
        from backend.agents.orchestrator import _extract_recommendation
        assert _extract_recommendation("I would buy here aggressively.") == "buy"
        assert _extract_recommendation("Enter on any dip below $450.") == "buy"

    def test_sell_detected(self):
        from backend.agents.orchestrator import _extract_recommendation
        assert _extract_recommendation("Time to trim this position.") == "sell"
        assert _extract_recommendation("Take profits now.") == "sell"

    def test_hold_detected(self):
        from backend.agents.orchestrator import _extract_recommendation
        assert _extract_recommendation("Hold for now, thesis intact.") == "hold"

    def test_avoid_detected(self):
        from backend.agents.orchestrator import _extract_recommendation
        assert _extract_recommendation("Avoid this setup entirely.") == "avoid"
        assert _extract_recommendation("Do not buy here.") == "avoid"

    def test_unknown_on_hedged_content(self):
        from backend.agents.orchestrator import _extract_recommendation
        # No decisive keyword — should fall through to unknown
        assert _extract_recommendation("The situation is developing.") == "unknown"
        assert _extract_recommendation("") == "unknown"

    def test_more_decisive_keyword_wins(self):
        """
        When both 'avoid' and 'hold' appear, 'avoid' wins — more decisive.
        Matches the priority order in the implementation.
        """
        from backend.agents.orchestrator import _extract_recommendation
        assert _extract_recommendation("Avoid for now, but hold existing positions") == "avoid"


class TestReactionGateIntegration:
    """
    End-to-end test that the material-change gate actually suppresses
    advisor calls in _trigger_news_reactions and creates the lightweight
    placeholder row instead.
    """

    @pytest.fixture(autouse=True)
    def _seed_watchlist(self, db):
        from backend.db.models import WatchlistTicker
        db.add(WatchlistTicker(ticker="NVDA"))
        db.commit()

    def test_cached_signal_skips_advisor_and_creates_placeholder(self, db):
        from datetime import datetime, timezone, timedelta
        # Prime the cache with a fresh "hold" at $450
        crud.upsert_advisor_cache(
            db,
            ticker="NVDA",
            price=450.0,
            recommendation="hold",
            rationale_summary="Holding through the range.",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock()  # should NOT be called

        with patch("backend.agents.orchestrator._fetch_current_price", return_value=453.0), \
             patch("backend.agents.orchestrator.get_broker", return_value=None):
            n = orch._trigger_news_reactions([{
                "ticker": "NVDA",
                "confidence": 0.9,
                "sentiment": "bullish",
                "headline": "NVDA breakout above resistance",
            }])

        assert n == 1
        orch.run_advisor.assert_not_called()

        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].trigger_reason == "cached_analysis"
        assert reactions[0].status == "read"  # no email, silent in UI
        assert "suppressed" in reactions[0].content.lower()
        assert "hold" in reactions[0].content.lower()

    def test_material_price_change_reruns_advisor(self, db):
        from datetime import datetime, timezone, timedelta
        crud.upsert_advisor_cache(
            db,
            ticker="NVDA",
            price=450.0,
            recommendation="hold",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Buy this aggressively"})
        )

        # 4% down from $450 = ~$432, well past the 3% threshold
        with patch("backend.agents.orchestrator._fetch_current_price", return_value=432.0), \
             patch("backend.agents.orchestrator.get_broker", return_value=None):
            n = orch._trigger_news_reactions([{
                "ticker": "NVDA",
                "confidence": 0.9,
                "sentiment": "bullish",
                "headline": "NVDA 4% drop",
            }])

        assert n == 1
        orch.run_advisor.assert_called_once()

        reactions = crud.list_news_reactions(db)
        assert reactions[0].trigger_reason == "watchlist"
        assert reactions[0].status == "unread"

        # Cache should have been updated with the new run
        cache = crud.get_advisor_cache(db, "NVDA")
        assert cache.last_price == 432.0
        assert cache.last_recommendation == "buy"

    def test_no_cache_runs_advisor_normally(self, db):
        """First-ever signal on a ticker — cache is empty, must run the advisor."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Hold for now"})
        )

        with patch("backend.agents.orchestrator._fetch_current_price", return_value=450.0), \
             patch("backend.agents.orchestrator.get_broker", return_value=None):
            orch._trigger_news_reactions([{
                "ticker": "NVDA",
                "confidence": 0.9,
                "sentiment": "bullish",
                "headline": "First signal",
            }])

        orch.run_advisor.assert_called_once()
        cache = crud.get_advisor_cache(db, "NVDA")
        assert cache is not None
        assert cache.last_recommendation == "hold"

    def test_price_fetch_failure_runs_advisor(self, db):
        """
        yfinance unavailable — can't judge material change, so run the
        advisor rather than skip on stale cache data. Erring on the side
        of accuracy over cost.
        """
        crud.upsert_advisor_cache(db, ticker="NVDA", price=450.0, recommendation="hold")
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Hold for now"})
        )

        with patch("backend.agents.orchestrator._fetch_current_price", return_value=None), \
             patch("backend.agents.orchestrator.get_broker", return_value=None):
            orch._trigger_news_reactions([{
                "ticker": "NVDA",
                "confidence": 0.9,
                "sentiment": "bullish",
                "headline": "Signal",
            }])

        orch.run_advisor.assert_called_once()


# ── 5. Intelligence pipeline folding (swing-trader mode) ────────────────────

class TestWeeklyPlanIntelligenceFolding:
    """
    When intelligence_pipeline_enabled=False, run_weekly_plan must call
    run_intelligence_pipeline() before generating the plan so signals stay
    fresh. When the flag is True (day-trader default), the pipeline runs
    on its own cadence and the weekly plan uses existing signals.
    """

    def test_folding_runs_pipeline_when_flag_disabled(self, db):
        orch = _make_orchestrator(db)
        orch.run_intelligence_pipeline = MagicMock(return_value={})
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Weekly plan body"})
        )

        with patch.object(settings, "intelligence_pipeline_enabled", False):
            result = orch.run_weekly_plan(trigger="test")

        orch.run_intelligence_pipeline.assert_called_once()
        orch.run_advisor.assert_called_once()
        assert result.success is True

    def test_folding_skipped_when_flag_enabled(self, db):
        orch = _make_orchestrator(db)
        orch.run_intelligence_pipeline = MagicMock(return_value={})
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Weekly plan body"})
        )

        with patch.object(settings, "intelligence_pipeline_enabled", True):
            orch.run_weekly_plan(trigger="test")

        # Pipeline should NOT run inside weekly plan when the standalone
        # scheduled job is enabled — that would double-fire it
        orch.run_intelligence_pipeline.assert_not_called()
        orch.run_advisor.assert_called_once()

    def test_weekly_plan_continues_on_folded_pipeline_failure(self, db):
        """
        Folded pipeline failure must not block the weekly plan — log the
        warning and press on with whatever signals already exist in the DB.
        """
        orch = _make_orchestrator(db)
        orch.run_intelligence_pipeline = MagicMock(
            side_effect=Exception("yfinance rate limited")
        )
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "Plan despite failure"})
        )

        with patch.object(settings, "intelligence_pipeline_enabled", False):
            result = orch.run_weekly_plan(trigger="test")

        orch.run_intelligence_pipeline.assert_called_once()
        orch.run_advisor.assert_called_once()
        assert result.success is True
