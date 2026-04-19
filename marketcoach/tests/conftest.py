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
def _hermetic_settings(monkeypatch):
    """
    Override settings that would otherwise make the test suite depend on
    (and side-effect onto) the developer's local .env. All four patches
    here exist because a specific test failure or real-world side effect
    happened without them:

      api_secret = ""
        Without this, a developer who sets API_SECRET locally (to match a
        Railway deploy config) makes every auth-protected endpoint return
        401 in tests — the clients don't send Authorization headers.

      alpaca_paper = True
      alpaca_live_confirmation = ""
        Without these, a developer with live mode configured locally
        (ALPACA_PAPER=false + the confirmation phrase) flips the endpoint's
        live-mode gate, turning tests that assume paper behaviour into
        403 "confirm_live_capital required" failures.

      notifications_enabled = False
        Without this, tests that exercise the full _check_price_alerts /
        _trigger_news_reactions paths (most of test_price_alerts.py and
        test_news_reactions.py) call the real notify() function, which
        reads the real Gmail SMTP creds from .env and SENDS REAL EMAILS
        on every pytest run. Discovered when the user reported a flood
        of test-fixture emails ("NVDA hit $505.00" x3, "News Alert:
        NVDA — Signal" etc.) arriving every time CI or a local suite
        ran. Tests that explicitly want to verify notify behaviour
        patch backend.notifications.notify directly — this global
        override just makes the default path silent.

    All settings are read at call time by their respective code paths
    (require_auth, is_live_mode, notify), so a runtime monkeypatch is
    enough; no module re-import needed.
    """
    monkeypatch.setattr(settings, "api_secret", "")
    monkeypatch.setattr(settings, "alpaca_paper", True)
    monkeypatch.setattr(settings, "alpaca_live_confirmation", "")
    monkeypatch.setattr(settings, "notifications_enabled", False)


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
