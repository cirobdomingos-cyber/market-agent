"""
Tests for the agent observability layer:
  - backend/agents/pricing.py — cost arithmetic + model fallback
  - GET /agent-stats — aggregation rollup endpoint

The wiring inside BaseAgent._record_call is exercised indirectly:
  - We don't make real Anthropic calls; the pricing math is pure
    Python and is the only thing the BaseAgent path could get wrong.
  - The endpoint is fed seeded AgentCall rows so the aggregation can
    be checked deterministically.

What we deliberately don't test here:
  - Telemetry must never break the agent loop. Verifying that requires
    a contrived "DB blows up at insert time" mock that would test the
    test framework more than the code. The try/except around the whole
    _record_call body is the contract; reading it once is enough.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.pricing import cost_usd
from backend.db import get_db
from backend.db.models import AgentCall, Base
from backend.main import app


# ── Pricing module ───────────────────────────────────────────────────────────


class TestPricing:
    def test_sonnet_input_only(self):
        # 1M input tokens at $3/Mtok = $3 exactly
        cost = cost_usd(
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        assert cost == pytest.approx(3.0, abs=1e-6)

    def test_sonnet_full_call(self):
        # Realistic mixed call. Numbers picked so the math is hand-checkable:
        # 1k input * $3/Mtok    = 0.003
        # 1k output * $15/Mtok  = 0.015
        # 1k cache_read * $0.30 = 0.0003
        # 1k cache_write * $3.75 = 0.00375
        # Total = 0.02205
        cost = cost_usd(
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=1000,
            cache_creation_tokens=1000,
        )
        assert cost == pytest.approx(0.02205, abs=1e-6)

    def test_haiku_is_cheaper_than_sonnet(self):
        """
        News agent runs on Haiku because it's ~3x cheaper for the same
        token volume. This regression catches accidentally-equal pricing
        if someone copy-pastes the Sonnet row when adding a model.
        """
        same_args = dict(
            input_tokens=10_000,
            output_tokens=2_000,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        haiku = cost_usd(model="claude-haiku-4-5-20251001", **same_args)
        sonnet = cost_usd(model="claude-sonnet-4-6", **same_args)
        assert haiku < sonnet, "Haiku must be priced cheaper than Sonnet"
        # And the gap is meaningful — not ~equal due to a typo.
        assert sonnet / haiku >= 2.5

    def test_unknown_model_falls_back_to_sonnet(self):
        """
        New model IDs ship before our pricing table catches up. The fallback
        must not throw and must return a non-zero cost — we'd rather
        over-estimate slightly than emit a free-looking row.
        """
        unknown = cost_usd(
            model="claude-fancy-new-model-x9",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        sonnet = cost_usd(
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        assert unknown == sonnet

    def test_zero_tokens_is_zero_cost(self):
        # Mocked test responses with no usage produce 0.0 — the row still
        # gets written but doesn't skew totals.
        assert cost_usd(
            model="claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        ) == 0.0


# ── /agent-stats endpoint ────────────────────────────────────────────────────


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


def _seed_call(
    db,
    *,
    agent: str,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost: float = 0.01,
    latency_ms: int = 1500,
    days_ago: float = 0,
):
    row = AgentCall(
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        run_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


class TestAgentStatsEndpoint:
    def test_empty_db_returns_zero_totals(self, client):
        """A brand-new install has no agent_calls rows yet — endpoint
        must still return a valid shape so the frontend can render."""
        resp = client.get("/agent-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["totals"]["calls"] == 0
        assert body["totals"]["cost_usd"] == 0.0
        assert body["totals"]["cache_hit_rate_pct"] is None
        assert body["by_agent"] == []
        assert body["daily"] == []

    def test_totals_sum_correctly(self, client, db):
        _seed_call(db, agent="NewsAgent", cost=0.10, input_tokens=5000, output_tokens=1000)
        _seed_call(db, agent="TradingAdvisorAgent", cost=0.50, input_tokens=10000, output_tokens=3000)
        resp = client.get("/agent-stats")
        body = resp.json()
        assert body["totals"]["calls"] == 2
        assert body["totals"]["cost_usd"] == pytest.approx(0.60, abs=1e-6)
        assert body["totals"]["input_tokens"] == 15000
        assert body["totals"]["output_tokens"] == 4000

    def test_by_agent_sorted_by_cost_desc(self, client, db):
        """The dashboard puts the biggest spender first — useful when
        scanning for "what's eating my budget" at a glance."""
        _seed_call(db, agent="NewsAgent", cost=0.10)
        _seed_call(db, agent="TradingAdvisorAgent", cost=0.50)
        _seed_call(db, agent="CoachAgent", cost=0.30)
        resp = client.get("/agent-stats")
        body = resp.json()
        agents_in_order = [b["agent"] for b in body["by_agent"]]
        assert agents_in_order == [
            "TradingAdvisorAgent",
            "CoachAgent",
            "NewsAgent",
        ]

    def test_cache_hit_rate_math(self, client, db):
        """
        Hit rate = cache_read / (input + cache_read + cache_create).
        With one call: 200 cache_read, 600 input, 200 cache_create
        → denominator 1000, rate 20.0%.
        """
        _seed_call(
            db,
            agent="TradingAdvisorAgent",
            input_tokens=600,
            cache_read_tokens=200,
            cache_creation_tokens=200,
        )
        resp = client.get("/agent-stats")
        body = resp.json()
        assert body["totals"]["cache_hit_rate_pct"] == pytest.approx(20.0, abs=0.01)

    def test_cache_hit_rate_none_when_no_caching(self, client, db):
        """If nothing cacheable was ever sent, denominator is zero and
        we render '—' on the frontend instead of '0%' (which would
        wrongly imply a bad cache, not "no cache at all")."""
        _seed_call(
            db,
            agent="NewsAgent",
            input_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        resp = client.get("/agent-stats")
        body = resp.json()
        assert body["totals"]["cache_hit_rate_pct"] is None

    def test_window_filters_old_rows(self, client, db):
        # Two recent calls + one outside the 7d window
        _seed_call(db, agent="NewsAgent", cost=0.10, days_ago=1)
        _seed_call(db, agent="NewsAgent", cost=0.10, days_ago=3)
        _seed_call(db, agent="NewsAgent", cost=99.0, days_ago=30)
        resp = client.get("/agent-stats?days=7")
        body = resp.json()
        assert body["totals"]["calls"] == 2
        assert body["totals"]["cost_usd"] == pytest.approx(0.20, abs=1e-6)
        # The 30d window picks all three up — proves the filter is
        # honouring the param, not just hardcoded to 7.
        resp30 = client.get("/agent-stats?days=60")
        assert resp30.json()["totals"]["calls"] == 3

    def test_daily_series_groups_by_date(self, client, db):
        _seed_call(db, agent="NewsAgent", cost=0.10, days_ago=0)
        _seed_call(db, agent="NewsAgent", cost=0.20, days_ago=0)
        _seed_call(db, agent="NewsAgent", cost=0.05, days_ago=2)
        resp = client.get("/agent-stats")
        daily = resp.json()["daily"]
        # Exactly two distinct days in the series
        assert len(daily) == 2
        # And the dollar sums are right per day
        amounts = sorted(d["cost_usd"] for d in daily)
        assert amounts == pytest.approx([0.05, 0.30], abs=1e-6)

    def test_avg_latency_excludes_nulls(self, client, db):
        """latency_ms is nullable; avg must skip the nulls, not treat
        them as zero (which would wrongly drag the average down)."""
        _seed_call(db, agent="NewsAgent", latency_ms=1000)
        _seed_call(db, agent="NewsAgent", latency_ms=3000)
        _seed_call(db, agent="NewsAgent", latency_ms=None)
        resp = client.get("/agent-stats")
        body = resp.json()
        # (1000 + 3000) / 2 = 2000
        assert body["totals"]["avg_latency_ms"] == 2000

    def test_days_param_validation(self, client):
        """days must be a sane positive int; the endpoint rejects 0
        and the FastAPI Query bounds reject anything > 90."""
        assert client.get("/agent-stats?days=0").status_code == 422
        assert client.get("/agent-stats?days=91").status_code == 422
