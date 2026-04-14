"""
Tests for the equity-history feature that backs the Dashboard chart.

Covers:
  1. CRUD — create/list/latest/time-range filtering
  2. Polling integration — every position-poll cycle writes an equity
     snapshot (including the first poll), failures are swallowed
  3. /equity-history endpoint — time range param, response shape
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.orchestrator import Orchestrator
from backend.config import settings
from backend.db import crud, get_db
from backend.db.models import Base, EquitySnapshot
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


def _make_orchestrator(db):
    orch = Orchestrator.__new__(Orchestrator)
    orch.db = db
    orch.client = MagicMock()
    return orch


# ── 1. CRUD ─────────────────────────────────────────────────────────────────

class TestEquitySnapshotCrud:
    def test_empty_state(self, db):
        assert crud.list_equity_snapshots(db) == []
        assert crud.get_latest_equity_snapshot(db) is None

    def test_create_and_list(self, db):
        crud.create_equity_snapshot(
            db, equity=100_000, buying_power=200_000, cash=100_000,
            portfolio_value=100_000,
        )
        rows = crud.list_equity_snapshots(db)
        assert len(rows) == 1
        assert rows[0].equity == 100_000

    def test_latest_returns_most_recent(self, db):
        import time
        crud.create_equity_snapshot(db, equity=100_000)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=101_000)
        latest = crud.get_latest_equity_snapshot(db)
        assert latest.equity == 101_000

    def test_list_orders_ascending(self, db):
        """The chart reads left-to-right, so CRUD returns oldest first."""
        import time
        crud.create_equity_snapshot(db, equity=100)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=200)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=150)
        rows = crud.list_equity_snapshots(db)
        equities = [r.equity for r in rows]
        assert equities == [100, 200, 150]

    def test_since_filter(self, db):
        """Snapshots older than the cutoff are excluded."""
        import time
        crud.create_equity_snapshot(db, equity=100)
        time.sleep(0.01)
        cutoff = datetime.now(timezone.utc)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=200)
        # Only the post-cutoff row should match
        recent = crud.list_equity_snapshots(db, since=cutoff)
        assert len(recent) == 1
        assert recent[0].equity == 200


# ── 2. Polling integration ──────────────────────────────────────────────────

class TestPollingWritesEquity:
    def setup_method(self):
        self._patches = [
            patch.object(settings, "position_reviews_enabled", True),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def _broker_with_account(self, equity=100_000):
        mock = MagicMock()
        mock.get_positions.return_value = []
        mock.get_account.return_value = {
            "equity": equity,
            "buying_power": equity * 2,
            "cash": equity,
            "portfolio_value": equity,
            "paper": True,
        }
        return mock

    def test_first_poll_writes_equity_snapshot(self, db):
        """First-poll-ever path still records equity even though it
        skips position change detection."""
        orch = _make_orchestrator(db)
        broker = self._broker_with_account(equity=100_000)
        with patch("backend.agents.orchestrator.get_broker", return_value=broker):
            orch._check_position_changes()
        rows = crud.list_equity_snapshots(db)
        assert len(rows) == 1
        assert rows[0].equity == 100_000

    def test_subsequent_polls_append_snapshots(self, db):
        """Each poll cycle writes a new snapshot — equity curve grows."""
        orch = _make_orchestrator(db)
        broker = self._broker_with_account(equity=100_000)
        with patch("backend.agents.orchestrator.get_broker", return_value=broker):
            orch._check_position_changes()
            broker.get_account.return_value = {
                "equity": 100_500, "buying_power": 201_000,
                "cash": 100_500, "portfolio_value": 100_500, "paper": True,
            }
            orch._check_position_changes()
            broker.get_account.return_value = {
                "equity": 99_800, "buying_power": 199_600,
                "cash": 99_800, "portfolio_value": 99_800, "paper": True,
            }
            orch._check_position_changes()
        equities = [r.equity for r in crud.list_equity_snapshots(db)]
        assert equities == [100_000, 100_500, 99_800]

    def test_disconnected_broker_no_snapshot(self, db):
        """If get_account returns the disconnected sentinel (no equity
        key), we swallow the failure and write nothing."""
        orch = _make_orchestrator(db)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_account.return_value = {
            "status": "disconnected", "message": "Broker down",
        }
        with patch("backend.agents.orchestrator.get_broker", return_value=broker):
            orch._check_position_changes()
        assert crud.list_equity_snapshots(db) == []

    def test_get_account_exception_is_non_fatal(self, db):
        """A broker error writing equity must not break the poll flow —
        position reviews are the primary purpose, equity is telemetry."""
        orch = _make_orchestrator(db)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_account.side_effect = Exception("broker timeout")
        with patch("backend.agents.orchestrator.get_broker", return_value=broker):
            # Should not raise
            orch._check_position_changes()
        assert crud.list_equity_snapshots(db) == []


# ── 3. /equity-history endpoint ─────────────────────────────────────────────

class TestEquityHistoryEndpoint:
    def test_empty_history(self, client):
        resp = client.get("/equity-history")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_rows_in_order(self, client, db):
        import time
        crud.create_equity_snapshot(db, equity=100_000)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=100_500)
        time.sleep(0.01)
        crud.create_equity_snapshot(db, equity=99_800)

        resp = client.get("/equity-history")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        # Oldest first
        assert body[0]["equity"] == 100_000
        assert body[1]["equity"] == 100_500
        assert body[2]["equity"] == 99_800
        # Response shape
        assert "timestamp" in body[0]
        assert "buying_power" in body[0]
        assert "cash" in body[0]

    def test_days_param_filters(self, client, db):
        """A snapshot from 2 days ago should be excluded when days=1."""
        from backend.db.models import EquitySnapshot as E
        # SQLite stores DateTime as naive, so we write naive here to match
        # what the production path does (polling writes naive via default).
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        db.add(E(equity=100_000, snapshot_at=old))
        db.add(E(equity=101_000, snapshot_at=recent))
        db.commit()

        resp = client.get("/equity-history?days=1")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["equity"] == 101_000

    def test_days_param_validation(self, client):
        # Too high
        resp = client.get("/equity-history?days=1000")
        assert resp.status_code == 422
        # Zero
        resp = client.get("/equity-history?days=0")
        assert resp.status_code == 422
