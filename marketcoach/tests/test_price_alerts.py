"""
Tests for the price alerts feature.

Covers:
  1. CRUD — create/list/group-by-ticker/mark-triggered/delete
  2. API — GET/POST/DELETE endpoints + validation
  3. Polling integration — orchestrator._check_price_alerts fires a
     news_reaction, marks the alert inactive, and does not re-fire on the
     next poll cycle
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import crud, get_db
from backend.db.models import Base, PriceAlert
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


# ── 1. CRUD ─────────────────────────────────────────────────────────────────

class TestPriceAlertCrud:
    def test_create_and_list(self, db):
        crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0,
            note="breakout", active=True,
        )
        crud.create_price_alert(
            db, ticker="GLD", condition="below", target_price=440.0,
            active=True,
        )
        rows = crud.list_price_alerts(db)
        assert len(rows) == 2
        assert {r.ticker for r in rows} == {"NVDA", "GLD"}

    def test_list_active_only(self, db):
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        crud.create_price_alert(
            db, ticker="GLD", condition="below", target_price=440.0, active=True,
        )
        crud.mark_alert_triggered(db, a1.id, 501.0)
        active = crud.list_price_alerts(db, active_only=True)
        assert len(active) == 1
        assert active[0].ticker == "GLD"

    def test_group_by_ticker_active_only(self, db):
        crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        crud.create_price_alert(
            db, ticker="NVDA", condition="below", target_price=480.0, active=True,
        )
        triggered = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=600.0, active=True,
        )
        crud.mark_alert_triggered(db, triggered.id, 601.0)
        crud.create_price_alert(
            db, ticker="GLD", condition="above", target_price=450.0, active=True,
        )
        grouped = crud.get_active_alerts_grouped_by_ticker(db)
        assert set(grouped.keys()) == {"NVDA", "GLD"}
        assert len(grouped["NVDA"]) == 2
        assert len(grouped["GLD"]) == 1

    def test_mark_triggered_sets_fields(self, db):
        a = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        won = crud.mark_alert_triggered(db, a.id, 502.5)
        assert won is True
        # Re-fetch to verify the row was persisted correctly
        db.refresh(a)
        assert a.active is False
        assert a.triggered_price == 502.5
        assert a.triggered_at is not None

    def test_mark_triggered_returns_false_on_second_call(self):
        """
        Race-condition guard: the second caller to mark_alert_triggered for
        the same alert ID must get False back. This is the mechanism that
        prevents a duplicate email when two APScheduler workers poll
        concurrently — both see the alert active, both call mark, but only
        one gets the row update and only one fires the downstream notification.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from backend.db.models import Base

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            a = crud.create_price_alert(
                s, ticker="NVDA", condition="above", target_price=500.0, active=True,
            )
            assert crud.mark_alert_triggered(s, a.id, 505.0) is True
            # Second call — alert is already triggered, must return False
            assert crud.mark_alert_triggered(s, a.id, 506.0) is False
        finally:
            s.close()
            Base.metadata.drop_all(bind=engine)

    def test_mark_triggered_returns_false_for_missing_alert(self, db):
        assert crud.mark_alert_triggered(db, "nonexistent-id", 100.0) is False

    def test_create_dedupes_active_duplicate(self, db):
        """
        POST /alerts is idempotent by (ticker, condition, target_price) when
        the existing alert is still active. Prevents the "user double-clicked
        Create and got three emails" failure mode.
        """
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        a2 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        assert a1.id == a2.id
        # Only ONE row exists in the DB
        all_rows = crud.list_price_alerts(db)
        assert len(all_rows) == 1

    def test_create_allows_new_alert_after_prior_fired(self, db):
        """
        Dedupe only collapses ACTIVE duplicates — once an alert has fired
        (active=False), creating a fresh one at the same level should succeed.
        Otherwise the user could never re-arm an alert they intentionally want
        to watch again.
        """
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        crud.mark_alert_triggered(db, a1.id, 505.0)
        # Now create a fresh one at the same level — should be allowed
        a2 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        assert a2.id != a1.id
        assert len(crud.list_price_alerts(db)) == 2

    def test_create_allows_different_thresholds(self, db):
        """
        Dedupe is strict equality on target_price — $500 and $501 are
        different alerts even on the same ticker + condition.
        """
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        a2 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=501.0, active=True,
        )
        assert a1.id != a2.id

    def test_create_different_condition_is_distinct(self, db):
        """An 'above $500' and a 'below $500' on the same ticker are NOT duplicates."""
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        a2 = crud.create_price_alert(
            db, ticker="NVDA", condition="below", target_price=500.0, active=True,
        )
        assert a1.id != a2.id

    def test_delete(self, db):
        a = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        assert crud.delete_price_alert(db, a.id) is True
        assert crud.list_price_alerts(db) == []

    def test_delete_missing_returns_false(self, db):
        assert crud.delete_price_alert(db, "nope-id") is False


# ── 2. API endpoints ────────────────────────────────────────────────────────

class TestPriceAlertApi:
    def test_get_empty(self, client):
        resp = client.get("/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_post_creates_alert(self, client, db):
        resp = client.post("/alerts", json={
            "ticker": "nvda",
            "condition": "above",
            "target_price": 500.0,
            "note": "breakout level",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "NVDA"  # uppercased
        assert body["condition"] == "above"
        assert body["target_price"] == 500.0
        assert body["active"] is True
        # Round-trip
        assert len(crud.list_price_alerts(db)) == 1

    def test_post_rejects_invalid_condition(self, client):
        resp = client.post("/alerts", json={
            "ticker": "NVDA",
            "condition": "equals",
            "target_price": 500.0,
        })
        assert resp.status_code == 422

    def test_post_rejects_non_positive_price(self, client):
        resp = client.post("/alerts", json={
            "ticker": "NVDA",
            "condition": "above",
            "target_price": 0,
        })
        assert resp.status_code == 422

    def test_post_rejects_bad_ticker(self, client):
        resp = client.post("/alerts", json={
            "ticker": "TOOLONG",
            "condition": "above",
            "target_price": 500.0,
        })
        assert resp.status_code == 422

    def test_get_filters_active_only(self, client, db):
        a1 = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        crud.create_price_alert(
            db, ticker="GLD", condition="below", target_price=440.0, active=True,
        )
        crud.mark_alert_triggered(db, a1.id, 501.0)
        resp = client.get("/alerts?active_only=true")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["ticker"] == "GLD"

    def test_delete_removes_alert(self, client, db):
        a = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        resp = client.delete(f"/alerts/{a.id}")
        assert resp.status_code == 200
        assert crud.list_price_alerts(db) == []

    def test_delete_404(self, client):
        resp = client.delete("/alerts/does-not-exist")
        assert resp.status_code == 404


# ── 3. Polling integration ─────────────────────────────────────────────────

class TestAlertPolling:
    """The 5-min position poll calls orchestrator._check_price_alerts. That
    method must: (a) fire on hit, (b) create a news_reaction with
    trigger_reason='price_alert', (c) deactivate the alert so it doesn't
    re-fire, (d) leave non-hit alerts untouched."""

    def _orch(self, db):
        # Build just enough of the orchestrator to call _check_price_alerts
        # without touching Anthropic, Alpaca, or IBKR.
        from backend.agents.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch.db = db
        return orch

    def test_above_hit_fires_reaction_and_deactivates(self, db):
        alert = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        orch = self._orch(db)
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"price": 505.0, "ticker": "NVDA"},
        ):
            fired = orch._check_price_alerts()
        assert fired == 1
        db.refresh(alert)
        assert alert.active is False
        assert alert.triggered_price == 505.0
        # news_reaction row created with the right trigger_reason
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        assert reactions[0].trigger_reason == "price_alert"
        assert reactions[0].ticker == "NVDA"

    def test_below_miss_does_nothing(self, db):
        alert = crud.create_price_alert(
            db, ticker="GLD", condition="below", target_price=440.0, active=True,
        )
        orch = self._orch(db)
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"price": 450.0, "ticker": "GLD"},
        ):
            fired = orch._check_price_alerts()
        assert fired == 0
        db.refresh(alert)
        assert alert.active is True
        assert crud.list_news_reactions(db) == []

    def test_triggered_alert_does_not_refire_next_cycle(self, db):
        alert = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        orch = self._orch(db)
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"price": 505.0, "ticker": "NVDA"},
        ):
            orch._check_price_alerts()
            # Second poll — price still above, but alert is inactive, so
            # no new reaction should be created.
            orch._check_price_alerts()
        reactions = crud.list_news_reactions(db)
        assert len(reactions) == 1
        db.refresh(alert)
        assert alert.active is False

    def test_one_quote_per_ticker_not_per_alert(self, db):
        # Two alerts on NVDA — only one market_data call should happen
        crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        crud.create_price_alert(
            db, ticker="NVDA", condition="below", target_price=400.0, active=True,
        )
        orch = self._orch(db)
        mock_quote = MagicMock(return_value={"price": 505.0, "ticker": "NVDA"})
        with patch("backend.tools.market_data.execute_market_data", mock_quote):
            orch._check_price_alerts()
        assert mock_quote.call_count == 1

    def test_race_loser_does_not_notify(self, db):
        """
        If mark_alert_triggered returns False (another worker already claimed
        the alert), orchestrator must NOT create a news_reactions row and must
        NOT send a notification email. This is the fix for the "3x emails for
        the same level" bug.
        """
        alert = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        orch = self._orch(db)

        # Patch mark_alert_triggered to simulate losing the race
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"price": 505.0, "ticker": "NVDA"},
        ), patch(
            "backend.agents.orchestrator.crud.mark_alert_triggered",
            return_value=False,
        ), patch("backend.notifications.notify") as mock_notify:
            fired = orch._check_price_alerts()

        assert fired == 0
        # No news_reactions row — the race-loser must be silent
        assert crud.list_news_reactions(db) == []
        # No email sent
        mock_notify.assert_not_called()

    def test_race_winner_notifies_once(self, db):
        """
        The winner of the atomic mark MUST create exactly one news_reactions
        row and send exactly one notification. Complements the race-loser
        test — together they prove the gate works in both directions.
        """
        alert = crud.create_price_alert(
            db, ticker="NVDA", condition="above", target_price=500.0, active=True,
        )
        orch = self._orch(db)

        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"price": 505.0, "ticker": "NVDA"},
        ), patch("backend.notifications.notify") as mock_notify:
            fired = orch._check_price_alerts()

        assert fired == 1
        assert len(crud.list_news_reactions(db)) == 1
        mock_notify.assert_called_once()
