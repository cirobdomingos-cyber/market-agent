"""
Tests for API routes using FastAPI's TestClient.

Overrides the DB dependency with an in-memory SQLite session so no real
database is needed. Tests verify route behavior, status codes, and validation.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.models import Base
from backend.db import crud
from backend.db import get_db
from backend.main import app

# StaticPool forces all connections to share one in-memory database.
# Without this, each connection gets its own empty database.
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_test_engine)


# -- Fixtures ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _setup_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=_test_engine)
    yield
    Base.metadata.drop_all(bind=_test_engine)


@pytest.fixture()
def db_session():
    session = _TestSession()
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    """TestClient with DB dependency overridden to use in-memory SQLite."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


# -- Health --------------------------------------------------------------------

class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# -- Signals -------------------------------------------------------------------

class TestSignalRoutes:
    def test_get_signals_empty(self, client):
        resp = client.get("/signals")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_signals_with_data(self, client, db_session):
        crud.create_signal(
            db_session, ticker="NVDA", sentiment="bullish", confidence=0.9,
            source="Reuters", headline="NVDA up",
        )
        resp = client.get("/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ticker"] == "NVDA"

    def test_get_signals_validates_limit(self, client):
        resp = client.get("/signals?limit=0")
        assert resp.status_code == 422

    def test_get_signals_validates_skip(self, client):
        resp = client.get("/signals?skip=-1")
        assert resp.status_code == 422

    def test_get_signals_validates_hours(self, client):
        resp = client.get("/signals?hours=0")
        assert resp.status_code == 422


# -- Theses --------------------------------------------------------------------

class TestThesisRoutes:
    def test_get_theses_empty(self, client):
        resp = client.get("/theses")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_theses_with_data(self, client, db_session):
        crud.create_thesis(
            db_session, ticker="AAPL", direction="bullish", confidence=0.7,
            timeframe="3-10 days", reasoning="Strong", key_risks='["risk1"]',
        )
        resp = client.get("/theses")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ticker"] == "AAPL"
        assert data[0]["key_risks"] == ["risk1"]

    def test_get_theses_validates_limit(self, client):
        resp = client.get("/theses?limit=999")
        assert resp.status_code == 422


# -- Accuracy ------------------------------------------------------------------

class TestAccuracyRoute:
    def test_accuracy_empty(self, client):
        resp = client.get("/accuracy")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# -- Chat ----------------------------------------------------------------------

class TestChatRoutes:
    def test_chat_validates_empty_message(self, client):
        resp = client.post("/chat", json={"session_id": "s1", "message": ""})
        assert resp.status_code == 422

    def test_chat_validates_session_id_format(self, client):
        resp = client.post("/chat", json={"session_id": "bad session!", "message": "hello"})
        assert resp.status_code == 422

    def test_chat_validates_message_too_long(self, client):
        resp = client.post("/chat", json={"session_id": "s1", "message": "x" * 4001})
        assert resp.status_code == 422

    def test_get_chat_history_empty(self, client):
        resp = client.get("/chat/nonexistent")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_chat_history_with_data(self, client, db_session):
        crud.create_message(db_session, session_id="s1", role="user", content="Hello")
        crud.create_message(db_session, session_id="s1", role="assistant", content="Hi!")
        resp = client.get("/chat/s1")
        data = resp.json()
        assert len(data) == 2
        assert data[0]["role"] == "user"


# -- Profile -------------------------------------------------------------------

class TestProfileRoutes:
    def test_get_profile_empty(self, client):
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert resp.json()["memories"] == []

    def test_set_and_get_profile(self, client):
        # Set a memory
        resp = client.post("/profile", json={
            "key": "risk_tolerance", "value": "moderate", "category": "profile"
        })
        assert resp.status_code == 200
        assert resp.json()["key"] == "risk_tolerance"

        # Get it back
        resp = client.get("/profile")
        memories = resp.json()["memories"]
        assert len(memories) == 1
        assert memories[0]["value"] == "moderate"

    def test_set_profile_validates_key(self, client):
        resp = client.post("/profile", json={
            "key": "bad key!", "value": "test", "category": "profile"
        })
        assert resp.status_code == 422

    def test_set_profile_validates_category(self, client):
        resp = client.post("/profile", json={
            "key": "test", "value": "test", "category": "invalid"
        })
        assert resp.status_code == 422


class TestWeeklyPlanRoutes:
    """GET /weekly-plan and GET /weekly-plan/latest should read from the DB
    without hitting the Anthropic API. The POST /weekly-plan/run endpoint is
    covered by a separate orchestrator unit test."""

    def test_latest_returns_404_when_empty(self, client):
        resp = client.get("/weekly-plan/latest")
        assert resp.status_code == 404

    def test_list_empty(self, client):
        resp = client.get("/weekly-plan")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_latest_returns_most_recent(self, client, db_session):
        crud.create_weekly_plan(
            db_session,
            session_id="weekly-plan-scheduled",
            content="# Old plan",
            status="completed",
            trigger="scheduled",
        )
        crud.create_weekly_plan(
            db_session,
            session_id="weekly-plan-scheduled",
            content="# New plan",
            status="completed",
            trigger="manual",
        )

        resp = client.get("/weekly-plan/latest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "# New plan"
        assert body["trigger"] == "manual"
        assert body["status"] == "completed"

    def test_list_orders_newest_first_and_respects_limit(self, client, db_session):
        for i in range(3):
            crud.create_weekly_plan(
                db_session,
                session_id="weekly-plan-scheduled",
                content=f"plan {i}",
                status="completed",
                trigger="scheduled",
            )

        resp = client.get("/weekly-plan?limit=2")
        assert resp.status_code == 200
        plans = resp.json()
        assert len(plans) == 2
        # Newest first
        assert plans[0]["content"] == "plan 2"
        assert plans[1]["content"] == "plan 1"


class TestMorningBriefRoutes:
    """Read endpoints — POST /morning-brief/run is covered by orchestrator tests
    since it would otherwise hit the real Anthropic API."""

    def test_latest_returns_404_when_empty(self, client):
        resp = client.get("/morning-brief/latest")
        assert resp.status_code == 404

    def test_list_empty(self, client):
        resp = client.get("/morning-brief")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_latest_returns_most_recent(self, client, db_session):
        crud.create_morning_brief(
            db_session,
            session_id="morning-brief-scheduled",
            content="# yesterday",
            status="completed",
            trigger="scheduled",
        )
        crud.create_morning_brief(
            db_session,
            session_id="morning-brief-scheduled",
            content="# today",
            status="completed",
            trigger="manual",
        )

        resp = client.get("/morning-brief/latest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "# today"
        assert body["trigger"] == "manual"
        assert body["status"] == "completed"

    def test_list_respects_limit(self, client, db_session):
        for i in range(5):
            crud.create_morning_brief(
                db_session,
                session_id="morning-brief-scheduled",
                content=f"brief {i}",
                status="completed",
                trigger="scheduled",
            )

        resp = client.get("/morning-brief?limit=3")
        assert resp.status_code == 200
        briefs = resp.json()
        assert len(briefs) == 3
        # Newest first
        assert briefs[0]["content"] == "brief 4"


class TestMarketDataRoutes:
    """
    HTTP surface over backend.tools.market_data. Tests mock execute_market_data
    so we never hit yfinance in CI — that layer has its own tests, here we
    only verify the HTTP wrapper (status codes, error mapping, param forwarding).
    """

    def test_quote_returns_data(self, client):
        from unittest.mock import patch
        fake = {
            "ticker": "NVDA",
            "name": "NVIDIA Corp",
            "price": 201.68,
            "change": 1.80,
            "change_percent": 0.90,
            "volume": 45_000_000,
            "market_cap": 5_000_000_000_000,
            "timestamp": "2026-04-18T12:00:00+00:00",
        }
        with patch("backend.tools.market_data.execute_market_data", return_value=fake):
            resp = client.get("/market-data/NVDA/quote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "NVDA"
        assert body["price"] == 201.68

    def test_quote_not_found_returns_404(self, client):
        from unittest.mock import patch
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"error": "No data found for ticker 'XYZZY'."},
        ):
            resp = client.get("/market-data/XYZZY/quote")
        assert resp.status_code == 404

    def test_quote_yfinance_failure_returns_502(self, client):
        from unittest.mock import patch
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"error": "Connection refused"},
        ):
            resp = client.get("/market-data/NVDA/quote")
        assert resp.status_code == 502

    def test_history_returns_prices_array(self, client):
        from unittest.mock import patch
        fake = {
            "ticker": "NVDA",
            "days_requested": 30,
            "days_returned": 22,
            "prices": [
                {"date": "2026-04-01", "close": 190.00, "volume": 40_000_000},
                {"date": "2026-04-02", "close": 192.50, "volume": 42_000_000},
            ],
            "timestamp": "2026-04-18T12:00:00+00:00",
        }
        with patch("backend.tools.market_data.execute_market_data", return_value=fake):
            resp = client.get("/market-data/NVDA/history?days=30")
        assert resp.status_code == 200
        body = resp.json()
        assert body["days_requested"] == 30
        assert len(body["prices"]) == 2

    def test_history_forwards_days_param(self, client):
        from unittest.mock import patch, MagicMock
        mock = MagicMock(return_value={
            "ticker": "SPY",
            "days_requested": 90,
            "days_returned": 65,
            "prices": [],
            "timestamp": "2026-04-18T12:00:00+00:00",
        })
        with patch("backend.tools.market_data.execute_market_data", mock):
            client.get("/market-data/SPY/history?days=90")
        mock.assert_called_once_with("price_history", ticker="SPY", days=90)

    def test_history_rejects_bad_days(self, client):
        """FastAPI Query validator should reject out-of-range days."""
        resp = client.get("/market-data/NVDA/history?days=0")
        assert resp.status_code == 422
        resp = client.get("/market-data/NVDA/history?days=500")
        assert resp.status_code == 422

    def test_history_not_found_returns_404(self, client):
        from unittest.mock import patch
        with patch(
            "backend.tools.market_data.execute_market_data",
            return_value={"error": "No price history available for 'XYZZY'."},
        ):
            resp = client.get("/market-data/XYZZY/history")
        assert resp.status_code == 404
