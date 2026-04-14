"""
Tests for the real-capital safety gate.

The dual-switch rule: live mode activates only when BOTH
  - settings.alpaca_paper == False
  - settings.alpaca_live_confirmation == LIVE_CONFIRMATION_PHRASE

These tests are the security guarantee — if any of them fail, the defence
has a hole.
"""

from unittest.mock import patch

import pytest

from backend.config import Settings, settings
from backend.agents.trading_advisor_agent import (
    _build_system_blocks,
    _mode_banner,
    _fmt_money,
    _format_positions,
    _format_signals,
    _format_theses,
)


def _make_settings(**overrides) -> Settings:
    """Build a Settings instance bypassing .env so tests are deterministic."""
    base = dict(
        anthropic_api_key="sk-test",
        alpaca_paper=True,
        alpaca_live_confirmation="",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestDualSwitchLiveMode:
    def test_default_is_paper(self):
        s = _make_settings()
        assert s.is_live_mode is False
        assert s.trading_mode == "paper"

    def test_paper_false_alone_is_not_live(self):
        """Flipping only alpaca_paper must NOT enable live. This is the
        whole point of the dual switch — a single misconfiguration cannot
        put real money at risk."""
        s = _make_settings(alpaca_paper=False)
        assert s.is_live_mode is False
        assert s.trading_mode == "paper"

    def test_confirmation_alone_is_not_live(self):
        """Setting the confirmation phrase without flipping alpaca_paper
        also must NOT enable live — the default must win when in doubt."""
        s = _make_settings(
            alpaca_paper=True,
            alpaca_live_confirmation=Settings.LIVE_CONFIRMATION_PHRASE,
        )
        assert s.is_live_mode is False

    def test_both_flags_enable_live(self):
        s = _make_settings(
            alpaca_paper=False,
            alpaca_live_confirmation=Settings.LIVE_CONFIRMATION_PHRASE,
        )
        assert s.is_live_mode is True
        assert s.trading_mode == "live"

    def test_wrong_confirmation_phrase_blocks_live(self):
        """Typos or close-but-wrong phrases must not activate live mode."""
        s = _make_settings(
            alpaca_paper=False,
            alpaca_live_confirmation="I understand this uses real money",  # close but wrong
        )
        assert s.is_live_mode is False

    def test_empty_confirmation_blocks_live(self):
        s = _make_settings(
            alpaca_paper=False,
            alpaca_live_confirmation="",
        )
        assert s.is_live_mode is False


class TestAdvisorPromptModeBanner:
    """The advisor system blocks must include the correct banner for each mode."""

    def _render(self, mode: str, enable_proposals: bool = False) -> str:
        """Render both blocks concatenated for substring assertions."""
        blocks = _build_system_blocks(
            mode=mode,
            enable_proposals=enable_proposals,
            account={
                "portfolio_value": 100_000.00,
                "buying_power": 200_000.00,
                "cash": 50_000.00,
            },
            positions=[],
            signals=[],
            theses=[],
        )
        return "\n".join(b["text"] for b in blocks)

    def test_paper_banner_rendered_for_paper_mode(self):
        rendered = self._render("paper")
        assert "PAPER TRADING" in rendered
        assert "REAL CAPITAL AT RISK" not in rendered
        # Mode label appears in the dynamic snapshot
        assert "Mode: paper" in rendered

    def test_live_banner_rendered_for_live_mode(self):
        rendered = self._render("live")
        assert "LIVE TRADING" in rendered
        assert "REAL CAPITAL AT RISK" in rendered
        assert "Mode: live" in rendered
        # The critical safety instructions must all be present
        assert "MORE conservative on position sizing" in rendered
        assert "dollar amount at risk" in rendered
        assert "revenge trading" in rendered

    def test_default_mode_in_banner_helper_falls_back_to_paper(self):
        # Unknown mode string → paper banner (fail-safe default)
        assert "PAPER TRADING" in _mode_banner("unknown")
        assert "PAPER TRADING" in _mode_banner("")


class TestModeEndpoint:
    """GET /mode must reflect the live settings state accurately."""

    def test_mode_endpoint_reports_paper_by_default(self, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.main import app

        # Ensure we're in a clean paper state
        monkeypatch.setattr(settings, "alpaca_paper", True)
        monkeypatch.setattr(settings, "alpaca_live_confirmation", "")

        client = TestClient(app)
        resp = client.get("/mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "paper"
        assert body["is_live"] is False
        assert body["paper_flag"] is True
        assert body["confirmation_set"] is False

    def test_mode_endpoint_reports_live_when_both_flags_set(self, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.main import app

        monkeypatch.setattr(settings, "alpaca_paper", False)
        monkeypatch.setattr(
            settings,
            "alpaca_live_confirmation",
            Settings.LIVE_CONFIRMATION_PHRASE,
        )

        client = TestClient(app)
        resp = client.get("/mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "live"
        assert body["is_live"] is True
        assert body["paper_flag"] is False
        assert body["confirmation_set"] is True

    def test_mode_endpoint_reports_paper_when_only_paper_flag_flipped(
        self, monkeypatch
    ):
        """The defence-in-depth case: paper=False but no confirmation → still paper."""
        from fastapi.testclient import TestClient
        from backend.main import app

        monkeypatch.setattr(settings, "alpaca_paper", False)
        monkeypatch.setattr(settings, "alpaca_live_confirmation", "")

        client = TestClient(app)
        resp = client.get("/mode")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "paper"
        assert resp.json()["is_live"] is False
