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
        updated = crud.mark_alert_triggered(db, a.id, 502.5)
        assert updated is not None
        assert updated.active is False
        assert updated.triggered_price == 502.5
        assert updated.triggered_at is not None

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
