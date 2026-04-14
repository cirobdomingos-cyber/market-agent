"""
Tests for the auto position-change reviews feature.

The diff logic is the critical correctness boundary. If it's wrong, you
either spam yourself with phantom "position changed" reviews on every poll
(noise) or miss real changes (silence). Both are worse than not having the
feature at all. These tests verify the four specific change types and the
edge cases around them.

Coverage:
  1. CRUD — snapshot persistence, latest-per-ticker query, baseline detection
  2. Diff classification — each of the 4 trigger reasons plus all the no-op
     cases that should NOT fire a review
  3. Orchestrator integration — first poll establishes baseline silently,
     second poll fires reviews on real changes, dedupe and per-poll cap apply,
     advisor failures persist with status=failed
  4. The Anthropic API is mocked at run_advisor — no real calls happen
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.base import AgentResult
from backend.agents.orchestrator import Orchestrator
from backend.config import settings
from backend.db import crud
from backend.db.models import Base, PositionSnapshot


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


def _make_orchestrator(db):
    """Build an Orchestrator without invoking Anthropic at construction."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.db = db
    orch.client = MagicMock()
    return orch


def _position(ticker, qty=10, avg_entry=100.0, current_price=110.0, pnl_pct=10.0):
    """Build a vendor-neutral position dict matching what BrokerClient returns."""
    return {
        "ticker": ticker,
        "qty": qty,
        "avg_entry": avg_entry,
        "current_price": current_price,
        "unrealised_pnl": (current_price - avg_entry) * qty,
        "unrealised_pnl_pct": pnl_pct,
    }


# ── 1. CRUD ─────────────────────────────────────────────────────────────────

class TestSnapshotCrud:
    def test_create_and_get_latest(self, db):
        crud.create_position_snapshots(db, [
            {"ticker": "NVDA", "qty": 10, "avg_entry": 400, "current_price": 420, "unrealised_pnl_pct": 5.0},
            {"ticker": "AAPL", "qty": 20, "avg_entry": 150, "current_price": 155, "unrealised_pnl_pct": 3.3},
        ])
        latest = crud.get_latest_position_snapshots(db)
        assert set(latest.keys()) == {"NVDA", "AAPL"}
        assert latest["NVDA"].qty == 10
        assert latest["AAPL"].avg_entry == 150

    def test_latest_returns_most_recent_per_ticker(self, db):
        import time
        # Write two snapshots for the same ticker, spaced apart so the
        # snapshot_at timestamps definitely differ. In production the polls
        # are minutes apart so this isn't an issue — only tests can hit
        # the same-millisecond edge case.
        crud.create_position_snapshots(db, [
            {"ticker": "NVDA", "qty": 5, "avg_entry": 400, "current_price": 410, "unrealised_pnl_pct": 2.5},
        ])
        time.sleep(0.01)
        crud.create_position_snapshots(db, [
            {"ticker": "NVDA", "qty": 10, "avg_entry": 405, "current_price": 415, "unrealised_pnl_pct": 2.4},
        ])
        latest = crud.get_latest_position_snapshots(db)
        assert latest["NVDA"].qty == 10  # the newer one wins

    def test_has_any_snapshot_false_when_empty(self, db):
        assert crud.has_any_position_snapshot(db) is False

    def test_has_any_snapshot_true_after_insert(self, db):
        crud.create_position_snapshots(db, [
            {"ticker": "NVDA", "qty": 1, "avg_entry": 100, "current_price": 100, "unrealised_pnl_pct": 0},
        ])
        assert crud.has_any_position_snapshot(db) is True

    def test_create_empty_list_is_noop(self, db):
        assert crud.create_position_snapshots(db, []) == 0
        assert crud.has_any_position_snapshot(db) is False


# ── 2. Diff classification (no Anthropic, no broker) ────────────────────────

class TestClassifyChange:
    def setup_method(self):
        self._patches = [
            patch.object(settings, "position_qty_change_threshold_pct", 5.0),
            patch.object(settings, "position_pnl_change_threshold_pct", 10.0),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def _classify(self, db, ticker, current, previous):
        orch = _make_orchestrator(db)
        return orch._classify_position_change(ticker, current, previous)

    def test_position_opened_when_no_previous(self, db):
        reason, _ = self._classify(db, "NVDA", _position("NVDA"), None)
        assert reason == "position_opened"

    def test_position_opened_when_previous_was_tombstone(self, db):
        """A previous qty=0 row (closed-position tombstone) followed by a
        new non-zero qty should be classified as a fresh open."""
        prev = PositionSnapshot(ticker="NVDA", qty=0)
        reason, _ = self._classify(db, "NVDA", _position("NVDA"), prev)
        assert reason == "position_opened"

    def test_position_closed_when_current_missing(self, db):
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, current_price=420)
        reason, _ = self._classify(db, "NVDA", None, prev)
        assert reason == "position_closed"

    def test_position_closed_when_qty_drops_to_zero(self, db):
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400)
        reason, _ = self._classify(db, "NVDA", _position("NVDA", qty=0), prev)
        assert reason == "position_closed"

    def test_position_changed_when_qty_increases_above_threshold(self, db):
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, unrealised_pnl_pct=5.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=20, avg_entry=405, pnl_pct=5.0), prev
        )
        assert reason == "position_changed"

    def test_position_changed_when_qty_decreases_above_threshold(self, db):
        prev = PositionSnapshot(ticker="NVDA", qty=20, avg_entry=400, unrealised_pnl_pct=5.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=10, avg_entry=400, pnl_pct=5.0), prev
        )
        assert reason == "position_changed"

    def test_qty_change_below_threshold_is_not_a_review(self, db):
        """4% qty change with default 5% threshold → no review."""
        prev = PositionSnapshot(ticker="NVDA", qty=100, avg_entry=400, unrealised_pnl_pct=5.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=104, avg_entry=400, pnl_pct=5.0), prev
        )
        assert reason is None

    def test_pnl_threshold_when_pnl_moves_above_threshold(self, db):
        """P&L moves from 2% → 13% (11pp) with 10pp threshold → fires."""
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, unrealised_pnl_pct=2.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=10, avg_entry=400, pnl_pct=13.0), prev
        )
        assert reason == "pnl_threshold"

    def test_pnl_move_below_threshold_is_not_a_review(self, db):
        """P&L moves from 2% → 9% (7pp) with 10pp threshold → no review."""
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, unrealised_pnl_pct=2.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=10, avg_entry=400, pnl_pct=9.0), prev
        )
        assert reason is None

    def test_no_change_returns_none(self, db):
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, unrealised_pnl_pct=5.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=10, avg_entry=400, pnl_pct=5.0), prev
        )
        assert reason is None

    def test_pnl_move_in_negative_direction_also_fires(self, db):
        """A position dropping from +5% to -10% (15pp move) is critical and
        must fire a review, not just upward moves."""
        prev = PositionSnapshot(ticker="NVDA", qty=10, avg_entry=400, unrealised_pnl_pct=5.0)
        reason, _ = self._classify(
            db, "NVDA", _position("NVDA", qty=10, avg_entry=400, pnl_pct=-10.0), prev
        )
        assert reason == "pnl_threshold"


# ── 3. Orchestrator integration ─────────────────────────────────────────────

class TestOrchestratorPolling:
    def setup_method(self):
        self._patches = [
            patch.object(settings, "position_reviews_enabled", True),
            patch.object(settings, "position_qty_change_threshold_pct", 5.0),
            patch.object(settings, "position_pnl_change_threshold_pct", 10.0),
            patch.object(settings, "position_review_dedupe_hours", 4),
            patch.object(settings, "position_reviews_per_poll_max", 5),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def _orch_with_mock_broker(self, db, positions):
        orch = _make_orchestrator(db)
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = positions
        orch.run_advisor = MagicMock(
            return_value=AgentResult(
                success=True, data={"reply": "Review markdown content"}
            )
        )
        return orch, mock_broker

    def test_disabled_returns_zero(self, db):
        with patch.object(settings, "position_reviews_enabled", False):
            orch, mock_broker = self._orch_with_mock_broker(db, [_position("NVDA")])
            with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
                assert orch._check_position_changes() == 0

    def test_no_broker_returns_zero(self, db):
        orch = _make_orchestrator(db)
        with patch("backend.agents.orchestrator.get_broker", return_value=None):
            assert orch._check_position_changes() == 0

    def test_first_poll_writes_baseline_no_reviews(self, db):
        """First poll ever: positions exist but we haven't seen them. Don't
        fire reviews — establish the baseline silently."""
        orch, mock_broker = self._orch_with_mock_broker(db, [
            _position("NVDA", qty=10),
            _position("AAPL", qty=20),
        ])
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            created = orch._check_position_changes()
        assert created == 0
        # But the snapshot WAS written
        latest = crud.get_latest_position_snapshots(db)
        assert set(latest.keys()) == {"NVDA", "AAPL"}
        # And no reviews exist
        assert crud.list_news_reactions(db) == []
        # And run_advisor was never called
        orch.run_advisor.assert_not_called()

    def test_second_poll_no_changes_no_reviews(self, db):
        """Baseline established, then poll again with the same positions.
        Should not fire any reviews."""
        orch, mock_broker = self._orch_with_mock_broker(db, [_position("NVDA", qty=10)])
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline
            orch.run_advisor.reset_mock()
            created = orch._check_position_changes()  # second poll
        assert created == 0
        orch.run_advisor.assert_not_called()

    def test_new_position_after_baseline_fires_review(self, db):
        """Baseline has nothing, then poll reveals a new position. Should
        fire one review with trigger_reason=position_opened."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )

        # First poll: no positions → baseline empty
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()

        # Second poll: NVDA appears
        mock_broker.get_positions.return_value = [_position("NVDA", qty=10)]
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            created = orch._check_position_changes()

        assert created == 1
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].ticker == "NVDA"
        assert reactions[0].trigger_reason == "position_opened"
        assert reactions[0].content == "review"
        assert reactions[0].status == "unread"

    def test_closed_position_fires_review_then_silent(self, db):
        """When a position disappears, fire one review. After that, the
        tombstone snapshot prevents repeated 'position closed' reviews on
        future polls."""
        orch, mock_broker = self._orch_with_mock_broker(db, [_position("NVDA", qty=10)])
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline
            mock_broker.get_positions.return_value = []
            orch.run_advisor.reset_mock()
            first_close = orch._check_position_changes()
            # Third poll, still nothing
            second_close = orch._check_position_changes()

        assert first_close == 1
        assert second_close == 0
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].trigger_reason == "position_closed"

    def test_dedupe_skips_recent_review_for_same_ticker(self, db):
        """If a review was just generated for NVDA, the next poll detecting
        another NVDA change inside the dedupe window should be skipped."""
        # Pre-populate a recent reaction for NVDA
        crud.create_news_reaction(
            db,
            ticker="NVDA",
            headline="prior",
            content="prior",
            trigger_reason="position_opened",
        )

        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock_broker = MagicMock()
        # Simulate: NVDA wasn't in baseline, now appears
        mock_broker.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline (empty)
            mock_broker.get_positions.return_value = [_position("NVDA")]
            created = orch._check_position_changes()
        assert created == 0  # dedupe skipped it
        # Still only the original reaction
        assert len(crud.list_news_reactions(db)) == 1

    def test_per_poll_cap_enforced(self, db):
        """Ten positions appear at once but the per-poll cap is 2.
        Only 2 reviews are created."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []

        with patch.object(settings, "position_reviews_per_poll_max", 2), \
             patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline
            mock_broker.get_positions.return_value = [
                _position("NVDA"), _position("AAPL"), _position("MSFT"),
                _position("GOOGL"), _position("TSLA"),
            ]
            created = orch._check_position_changes()

        assert created == 2

    def test_advisor_failure_persisted_as_failed(self, db):
        """A failed advisor call still creates a reaction row with status=failed
        so the user can see the error in the UI."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(
                success=False, data={"reply": ""}, error="rate limit"
            )
        )
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline
            mock_broker.get_positions.return_value = [_position("NVDA")]
            created = orch._check_position_changes()

        assert created == 1
        r = crud.list_news_reactions(db)[0]
        assert r.status == "failed"
        assert r.error == "rate limit"

    def test_review_uses_position_review_session_id(self, db):
        """Background-generated reviews must use a dedicated session_id so
        they never pollute the user-facing /advisor chat history."""
        orch = _make_orchestrator(db)
        orch.run_advisor = MagicMock(
            return_value=AgentResult(success=True, data={"reply": "review"})
        )
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []
        with patch("backend.agents.orchestrator.get_broker", return_value=mock_broker):
            orch._check_position_changes()  # baseline
            mock_broker.get_positions.return_value = [_position("NVDA")]
            orch._check_position_changes()

        call_args = orch.run_advisor.call_args
        ctx = call_args[0][0] if call_args[0] else call_args[1]
        assert ctx["session_id"] == settings.position_review_session_id
        # And critically, enable_trade_proposals must be False — background
        # reviews never emit click-to-execute trade cards
        assert ctx["enable_trade_proposals"] is False
