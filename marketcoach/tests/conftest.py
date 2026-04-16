"""
Shared fixtures for MarketCoach tests.

Uses an in-memory SQLite database so tests are fast, isolated, and need no cleanup.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.db.models import Base


@pytest.fixture(autouse=True)
def _force_dev_mode_auth(monkeypatch):
    """
    Force API_SECRET='' for every test so the suite is hermetic to the
    developer's local .env. Without this, setting API_SECRET locally (e.g.
    to match a Railway deploy config) would make every auth-protected
    endpoint return 401 in tests, even though the test clients don't
    send Authorization headers.

    require_auth reads settings.api_secret at request time, so a runtime
    monkeypatch is sufficient — no need to re-import backend.main.
    """
    monkeypatch.setattr(settings, "api_secret", "")


@pytest.fixture()
def db():
    """Yield a SQLAlchemy session backed by an in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
