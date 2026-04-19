"""
Tests for agent base class helpers.

Tests _extract_json and _serialise_content without making real API calls.
These are the most important unit-testable parts of the agent layer.
"""

import pytest

from backend.agents.base import BaseAgent, AgentResult
from backend.agents.trading_advisor_agent import (
    _build_system_blocks,
    _fmt_money,
    _format_positions,
    _format_theses,
    DYNAMIC_CONTEXT_TEMPLATE,
    STABLE_INTRO_FRAMEWORK,
)
from backend.tools.broker_tool import (
    BROKER_READ_TOOL,
    _READ_ONLY_ACTIONS,
    execute_broker_read_tool,
)


class TestExtractJson:
    """Test the JSON extraction from Claude responses."""

    def test_raw_json(self):
        text = '{"signals": [{"ticker": "NVDA"}]}'
        result = BaseAgent._extract_json(text)
        assert result == {"signals": [{"ticker": "NVDA"}]}

    def test_code_fenced_json(self):
        text = 'Here is the analysis:\n```json\n{"theses": []}\n```\nDone.'
        result = BaseAgent._extract_json(text)
        assert result == {"theses": []}

    def test_code_fence_without_language(self):
        text = '```\n{"key": "value"}\n```'
        result = BaseAgent._extract_json(text)
        assert result == {"key": "value"}

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            BaseAgent._extract_json("not json at all")

    def test_nested_json(self):
        text = '```json\n{"a": {"b": [1, 2, 3]}}\n```'
        result = BaseAgent._extract_json(text)
        assert result["a"]["b"] == [1, 2, 3]


class TestSerialiseContent:
    """Test content block serialisation for message history."""

    def test_dict_passthrough(self):
        blocks = [{"type": "text", "text": "hello"}]
        result = BaseAgent._serialise_content(blocks)
        assert result == [{"type": "text", "text": "hello"}]

    def test_pydantic_model_dump(self):
        """Objects with model_dump() should be serialised via that method."""
        class FakeBlock:
            def model_dump(self):
                return {"type": "text", "text": "from pydantic"}

        result = BaseAgent._serialise_content([FakeBlock()])
        assert result == [{"type": "text", "text": "from pydantic"}]


class TestAgentResult:
    def test_defaults(self):
        result = AgentResult(success=True, data={"key": "value"})
        assert result.success is True
        assert result.error is None
        assert result.run_at is not None

    def test_failure(self):
        result = AgentResult(success=False, data={}, error="something broke")
        assert result.success is False
        assert result.error == "something broke"


class TestTradingAdvisorPrompt:
    """Verify the trading advisor's prompt helpers and template rendering."""

    def test_fmt_money_float(self):
        assert _fmt_money(12345.6) == "$12,345.60"

    def test_fmt_money_missing(self):
        assert _fmt_money(None) == "not connected"

    def test_format_positions_empty(self):
        assert _format_positions([]) == "No open positions."

    def test_format_positions_renders_ticker_and_pnl(self):
        out = _format_positions([
            {"ticker": "NVDA", "qty": 10, "avg_entry": 400.0,
             "current_price": 450.0, "unrealised_pnl_pct": 12.5},
        ])
        assert "NVDA" in out
        assert "12.5" in out

    def test_format_theses_empty(self):
        assert _format_theses([]) == "No open theses."

    def test_dynamic_template_renders_with_all_fields(self):
        """The dynamic context template must format cleanly with the runtime values."""
        rendered = DYNAMIC_CONTEXT_TEMPLATE.format(
            mode_label="paper",
            portfolio_value="$100,000.00",
            buying_power="$200,000.00",
            cash="$50,000.00",
            positions="  NVDA: 10 @ avg $400",
            signals="  TSLA [bullish]: earnings beat",
            theses="  AAPL (LONG, 0.7): iPhone cycle",
        )
        assert "$100,000.00" in rendered
        assert "NVDA" in rendered
        assert "TSLA" in rendered
        assert "AAPL" in rendered

    def test_stable_prefix_contains_framework_anchors(self):
        """The cacheable prefix must contain the full reasoning framework so that
        Claude can do its job from cache hits alone."""
        assert "Layer 1 — Macro Regime" in STABLE_INTRO_FRAMEWORK
        assert "Layer 4 — Trade Decision Matrix" in STABLE_INTRO_FRAMEWORK
        assert "2% rule" in STABLE_INTRO_FRAMEWORK
        assert "broker_account" in STABLE_INTRO_FRAMEWORK
        # And the dynamic-only fields must NOT be in it
        assert "{portfolio_value}" not in STABLE_INTRO_FRAMEWORK
        assert "{positions}" not in STABLE_INTRO_FRAMEWORK


class TestBrokerReadTool:
    """Broker-agnostic read-only tool must refuse writes and expose the right surface."""

    def test_tool_name_is_broker_account(self):
        """Renamed from alpaca_account when we added the broker abstraction so
        the tool name doesn't lie about which provider it talks to."""
        assert BROKER_READ_TOOL["name"] == "broker_account"

    def test_tool_schema_enum_is_read_only(self):
        allowed = BROKER_READ_TOOL["input_schema"]["properties"]["action"]["enum"]
        assert set(allowed) == {"get_positions", "get_account", "get_order_history"}
        assert "place_order" not in allowed
        assert "close_position" not in allowed

    def test_read_only_actions_constant_matches_schema(self):
        """_READ_ONLY_ACTIONS must stay in sync with the tool schema enum."""
        schema_enum = set(
            BROKER_READ_TOOL["input_schema"]["properties"]["action"]["enum"]
        )
        assert _READ_ONLY_ACTIONS == schema_enum

    def test_write_action_rejected(self):
        """place_order must be rejected without touching the broker."""
        result = execute_broker_read_tool("place_order", ticker="NVDA", qty=1, side="buy")
        assert "error" in result
        assert "read-only" in result["error"].lower()

    def test_close_position_rejected(self):
        result = execute_broker_read_tool("close_position", ticker="NVDA")
        assert "error" in result
        assert "read-only" in result["error"].lower()

    def test_read_action_with_no_broker_returns_clean_error(self):
        """With no broker initialised, get_positions should surface a clear error."""
        from backend.brokers.factory import reset_broker
        reset_broker()
        result = execute_broker_read_tool("get_positions")
        assert "error" in result
        assert "not initialised" in result["error"].lower()


class TestPerAgentModelOverride:
    """
    Regression guard: per-agent MODEL overrides must win over the module-level
    default. The NewsAgent in particular is expected to run on Haiku (~10x
    cheaper than Sonnet) because news extraction is a structured task where
    the smaller model performs at parity. This test locks in that override
    so a future refactor can't silently put NewsAgent back on Sonnet.
    """

    def test_base_agent_defaults_to_module_model(self):
        from backend.agents.base import BaseAgent, MODEL

        assert BaseAgent.MODEL == MODEL
        assert BaseAgent.MODEL == "claude-sonnet-4-6"

    def test_news_agent_overrides_to_haiku(self):
        from backend.agents.news_agent import NewsAgent

        assert NewsAgent.MODEL == "claude-haiku-4-5-20251001"

    def test_reasoning_agents_stay_on_sonnet(self):
        """
        Reasoning-heavy agents (analysis, trade ideas, memory, coach,
        advisor) should NOT accidentally inherit a Haiku override from
        somewhere. They stay on the Sonnet default unless explicitly
        overridden with a deliberate comment justifying the change.
        """
        from backend.agents.analysis_agent import AnalysisAgent
        from backend.agents.trade_idea_agent import TradeIdeaAgent
        from backend.agents.memory_agent import MemoryAgent
        from backend.agents.coach_agent import CoachAgent
        from backend.agents.trading_advisor_agent import TradingAdvisorAgent

        for cls in [AnalysisAgent, TradeIdeaAgent, MemoryAgent, CoachAgent, TradingAdvisorAgent]:
            assert cls.MODEL == "claude-sonnet-4-6", (
                f"{cls.__name__} should use Sonnet — reasoning quality matters"
            )
