"""
Tests for backend.db.crud — the data access layer.

These are pure DB tests with no external dependencies. They verify that every
CRUD function correctly reads and writes to SQLAlchemy models.
"""

from datetime import datetime, timedelta, timezone

from backend.db import crud
from backend.db.models import Thesis


# -- Signals -------------------------------------------------------------------

class TestSignals:
    def test_create_signal(self, db):
        signal = crud.create_signal(
            db, ticker="NVDA", sentiment="bullish", confidence=0.85,
            source="Reuters", headline="NVDA beats earnings",
        )
        assert signal.id is not None
        assert signal.ticker == "NVDA"
        assert signal.sentiment == "bullish"
        assert signal.confidence == 0.85

    def test_create_signals_batch(self, db):
        data = [
            dict(ticker="AAPL", sentiment="bullish", confidence=0.7, source="WSJ", headline="Apple up"),
            dict(ticker="MSFT", sentiment="neutral", confidence=0.5, source="CNBC", headline="MSFT flat"),
        ]
        count = crud.create_signals_batch(db, data)
        assert count == 2

        page = crud.get_signals_page(db, skip=0, limit=10)
        assert len(page) == 2

    def test_get_recent_signals_filters_by_hours(self, db):
        crud.create_signal(
            db, ticker="SPY", sentiment="bearish", confidence=0.6,
            source="Bloomberg", headline="Market drop",
        )
        signals = crud.get_recent_signals(db, hours=1)
        assert len(signals) == 1
        assert signals[0].ticker == "SPY"

    def test_get_recent_signals_filters_by_ticker(self, db):
        crud.create_signal(
            db, ticker="AAPL", sentiment="bullish", confidence=0.7,
            source="WSJ", headline="Apple up",
        )
        crud.create_signal(
            db, ticker="MSFT", sentiment="neutral", confidence=0.5,
            source="CNBC", headline="MSFT flat",
        )
        result = crud.get_recent_signals(db, hours=1, ticker="AAPL")
        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_get_signals_page_pagination(self, db):
        for i in range(5):
            crud.create_signal(
                db, ticker=f"T{i}", sentiment="neutral", confidence=0.5,
                source="test", headline=f"headline {i}",
            )
        page = crud.get_signals_page(db, skip=2, limit=2)
        assert len(page) == 2


# -- Theses --------------------------------------------------------------------

class TestTheses:
    def test_create_thesis(self, db):
        thesis = crud.create_thesis(
            db, ticker="TSLA", direction="bearish", confidence=0.65,
            timeframe="3-10 days", reasoning="Overvalued", key_risks='["macro risk"]',
        )
        assert thesis.id is not None
        assert thesis.direction == "bearish"
        assert thesis.resolved_at is None

    def test_create_theses_batch(self, db):
        data = [
            dict(ticker="NVDA", direction="bullish", confidence=0.8,
                 timeframe="1-3 days", reasoning="Strong", key_risks='[]'),
            dict(ticker="META", direction="neutral", confidence=0.5,
                 timeframe="2-4 weeks", reasoning="Mixed", key_risks='[]'),
        ]
        count = crud.create_theses_batch(db, data)
        assert count == 2

    def test_get_open_theses_excludes_resolved(self, db):
        crud.create_thesis(
            db, ticker="AAPL", direction="bullish", confidence=0.7,
            timeframe="1-3 days", reasoning="Good", key_risks='[]',
        )
        resolved = crud.create_thesis(
            db, ticker="MSFT", direction="bearish", confidence=0.6,
            timeframe="1-3 days", reasoning="Bad", key_risks='[]',
        )
        crud.resolve_thesis(db, resolved.id, outcome="correct", accuracy=0.9)

        open_theses = crud.get_open_theses(db)
        assert len(open_theses) == 1
        assert open_theses[0].ticker == "AAPL"

    def test_get_open_theses_respects_limit(self, db):
        for i in range(5):
            crud.create_thesis(
                db, ticker=f"T{i}", direction="bullish", confidence=0.5,
                timeframe="1d", reasoning="r", key_risks='[]',
            )
        result = crud.get_open_theses(db, limit=3)
        assert len(result) == 3

    def test_resolve_thesis(self, db):
        thesis = crud.create_thesis(
            db, ticker="GOOGL", direction="bullish", confidence=0.8,
            timeframe="3-10 days", reasoning="Strong earnings", key_risks='[]',
        )
        resolved = crud.resolve_thesis(db, thesis.id, outcome="correct", accuracy=0.95)
        assert resolved.outcome == "correct"
        assert resolved.resolved_at is not None

    def test_resolve_thesis_not_found(self, db):
        result = crud.resolve_thesis(db, "nonexistent-id", outcome="correct", accuracy=0.5)
        assert result is None


# -- Accuracy ------------------------------------------------------------------

class TestAccuracy:
    def test_accuracy_stats_empty(self, db):
        stats = crud.get_accuracy_stats(db)
        assert stats["total"] == 0
        assert stats["accuracy_pct"] is None

    def test_accuracy_stats_with_data(self, db):
        for i in range(3):
            t = crud.create_thesis(
                db, ticker=f"T{i}", direction="bullish", confidence=0.7,
                timeframe="1d", reasoning="r", key_risks='[]',
            )
            outcome = "correct" if i < 2 else "incorrect"
            crud.resolve_thesis(db, t.id, outcome=outcome, accuracy=0.8 if i < 2 else 0.2)

        stats = crud.get_accuracy_stats(db)
        assert stats["total"] == 3
        assert stats["correct"] == 2
        assert stats["incorrect"] == 1
        assert stats["accuracy_pct"] == 66.7


# -- Chat messages -------------------------------------------------------------

class TestChatMessages:
    def test_create_and_get_messages(self, db):
        crud.create_message(db, session_id="s1", role="user", content="Hello")
        crud.create_message(db, session_id="s1", role="assistant", content="Hi!")
        crud.create_message(db, session_id="s2", role="user", content="Other session")

        msgs = crud.get_session_messages(db, "s1")
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_get_session_messages_empty(self, db):
        msgs = crud.get_session_messages(db, "nonexistent")
        assert msgs == []


# -- Positions -----------------------------------------------------------------

# -- User memories -------------------------------------------------------------

class TestUserMemories:
    def test_upsert_create(self, db):
        mem = crud.upsert_memory(db, key="risk_tolerance", value="moderate", category="profile")
        assert mem.key == "risk_tolerance"
        assert mem.value == "moderate"
        assert mem.category == "profile"

    def test_upsert_update(self, db):
        crud.upsert_memory(db, key="risk_tolerance", value="low", category="profile")
        updated = crud.upsert_memory(db, key="risk_tolerance", value="high", category="profile")
        assert updated.value == "high"

        # Should still be only one memory with this key
        all_mems = crud.get_all_memories(db)
        risk_mems = [m for m in all_mems if m.key == "risk_tolerance"]
        assert len(risk_mems) == 1

    def test_get_memory(self, db):
        crud.upsert_memory(db, key="sector", value="tech", category="preference")
        mem = crud.get_memory(db, "sector")
        assert mem is not None
        assert mem.value == "tech"

    def test_get_memory_not_found(self, db):
        assert crud.get_memory(db, "nonexistent") is None

    def test_get_memories_by_category(self, db):
        crud.upsert_memory(db, key="risk", value="low", category="profile")
        crud.upsert_memory(db, key="budget", value="10k", category="profile")
        crud.upsert_memory(db, key="sector", value="tech", category="preference")

        profile = crud.get_memories_by_category(db, "profile")
        assert len(profile) == 2
        prefs = crud.get_memories_by_category(db, "preference")
        assert len(prefs) == 1

    def test_get_all_memories(self, db):
        crud.upsert_memory(db, key="a", value="1", category="profile")
        crud.upsert_memory(db, key="b", value="2", category="preference")
        assert len(crud.get_all_memories(db)) == 2

    def test_delete_memory(self, db):
        crud.upsert_memory(db, key="to_delete", value="bye", category="general")
        assert crud.delete_memory(db, "to_delete") is True
        assert crud.get_memory(db, "to_delete") is None

    def test_delete_memory_not_found(self, db):
        assert crud.delete_memory(db, "nonexistent") is False


# -- Positions -----------------------------------------------------------------

class TestPositions:
    def test_upsert_position(self, db):
        pos = crud.upsert_position(
            db, ticker="AAPL", qty=10.0, entry_price=175.50,
        )
        assert pos.id is not None
        assert pos.is_paper is True  # default

    def test_get_open_positions_paper_filter(self, db):
        crud.upsert_position(db, ticker="AAPL", qty=10.0, entry_price=175.50, is_paper=True)
        crud.upsert_position(db, ticker="MSFT", qty=5.0, entry_price=400.0, is_paper=False)

        paper = crud.get_open_positions(db, paper_only=True)
        assert len(paper) == 1
        assert paper[0].ticker == "AAPL"

        all_positions = crud.get_open_positions(db, paper_only=False)
        assert len(all_positions) == 2


# -- Weekly plans --------------------------------------------------------------

class TestWeeklyPlans:
    def test_create_and_get_latest(self, db):
        crud.create_weekly_plan(
            db,
            session_id="weekly-plan-scheduled",
            content="first",
            status="completed",
            trigger="scheduled",
        )
        crud.create_weekly_plan(
            db,
            session_id="weekly-plan-scheduled",
            content="second",
            status="completed",
            trigger="manual",
        )

        latest = crud.get_latest_weekly_plan(db)
        assert latest is not None
        assert latest.content == "second"
        assert latest.trigger == "manual"

    def test_get_latest_returns_none_when_empty(self, db):
        assert crud.get_latest_weekly_plan(db) is None

    def test_list_weekly_plans_respects_limit_and_order(self, db):
        for i in range(5):
            crud.create_weekly_plan(
                db,
                session_id="weekly-plan-scheduled",
                content=f"plan-{i}",
                status="completed",
                trigger="scheduled",
            )

        plans = crud.list_weekly_plans(db, limit=3)
        assert len(plans) == 3
        # Newest first
        assert plans[0].content == "plan-4"
        assert plans[1].content == "plan-3"

    def test_failed_plan_records_error(self, db):
        plan = crud.create_weekly_plan(
            db,
            session_id="weekly-plan-scheduled",
            content="",
            status="failed",
            error="rate limited",
            trigger="scheduled",
        )
        assert plan.status == "failed"
        assert plan.error == "rate limited"


# -- Morning briefs ------------------------------------------------------------

class TestMorningBriefs:
    def test_create_and_get_latest(self, db):
        crud.create_morning_brief(
            db,
            session_id="morning-brief-scheduled",
            content="first brief",
            status="completed",
            trigger="scheduled",
        )
        crud.create_morning_brief(
            db,
            session_id="morning-brief-scheduled",
            content="second brief",
            status="completed",
            trigger="manual",
        )

        latest = crud.get_latest_morning_brief(db)
        assert latest is not None
        assert latest.content == "second brief"
        assert latest.trigger == "manual"

    def test_get_latest_returns_none_when_empty(self, db):
        assert crud.get_latest_morning_brief(db) is None

    def test_list_respects_limit_and_order(self, db):
        for i in range(5):
            crud.create_morning_brief(
                db,
                session_id="morning-brief-scheduled",
                content=f"brief-{i}",
                status="completed",
                trigger="scheduled",
            )

        briefs = crud.list_morning_briefs(db, limit=3)
        assert len(briefs) == 3
        assert briefs[0].content == "brief-4"
        assert briefs[1].content == "brief-3"

    def test_failed_brief_persisted(self, db):
        brief = crud.create_morning_brief(
            db,
            session_id="morning-brief-scheduled",
            content="",
            status="failed",
            error="rate limited",
            trigger="scheduled",
        )
        assert brief.status == "failed"
        assert brief.error == "rate limited"
        # Failed briefs should still be returned by latest — UI needs to
        # show the failure, not silently hide it.
        assert crud.get_latest_morning_brief(db).id == brief.id
