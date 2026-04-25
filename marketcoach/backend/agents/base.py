"""
BaseAgent and AgentResult — shared contract for all MarketCoach agents.

Every agent:
  - Receives a context dict (portfolio, watchlist, etc.)
  - Returns an AgentResult with typed .data
  - Runs the Claude agentic loop internally via _agentic_loop()
  - Declares which tools it supports in _execute_tool()

Portfolio signal for interviewers:
  The agentic loop pattern (send → tool_use → tool_result → loop) is the
  standard pattern for all Claude tool-use agents. Knowing this loop cold
  is table stakes for any LLM engineering role.
"""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional, Union

import anthropic
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Default model for every agent. Subclasses can override the class-level
# BaseAgent.MODEL attribute to use a different tier for their task — e.g.
# NewsAgent uses Haiku because news extraction is a structured task where
# the smaller model is ~10x cheaper with no measurable quality loss. The
# advisor / analysis / trade-idea agents keep this Sonnet default because
# they do multi-step reasoning where the bigger model earns its cost.
MODEL = "claude-sonnet-4-6"
MAX_TOOL_ITERATIONS = 10

logger = logging.getLogger(__name__)


class AgentResult(BaseModel):
    success: bool
    data: Any
    error: Optional[str] = None
    run_at: datetime = None

    def model_post_init(self, __context: Any) -> None:
        if self.run_at is None:
            self.run_at = datetime.now(timezone.utc)


class BaseAgent(ABC):
    """
    Abstract base for all MarketCoach agents.

    Subclasses must implement:
      - run(context) → AgentResult
      - _execute_tool(name, input) → str   (if the agent uses tools)

    Subclasses MAY override:
      - MODEL — the Anthropic model ID used for this agent's calls.
                Defaults to module-level MODEL (Sonnet). Override with a
                class-level attribute, e.g.:
                    class MyAgent(BaseAgent):
                        MODEL = "claude-haiku-4-5-20251001"
    """

    MODEL: str = MODEL  # class-level; subclasses override for per-agent tiering

    def __init__(self, db: Session, client: anthropic.Anthropic):
        self.db = db
        self.client = client

    @abstractmethod
    def run(self, context: dict) -> AgentResult:
        raise NotImplementedError

    # ── Agentic loop ──────────────────────────────────────────────────────────

    def _agentic_loop(
        self,
        system: Union[str, list[dict]],
        messages: list[dict],
        tools: list[dict],
        max_iterations: int = MAX_TOOL_ITERATIONS,
    ) -> anthropic.types.Message:
        """
        Run the standard Claude tool-use loop until stop_reason == 'end_turn'.

        Flow per iteration:
          1. Call Claude with current messages + tools
          2. If stop_reason == 'end_turn' → return response
          3. If stop_reason == 'tool_use' → execute tools, append results, repeat
          4. Raise after max_iterations to prevent runaway spend
        """
        for iteration in range(max_iterations):
            response = self._call_api(system=system, messages=messages, tools=tools)

            logger.debug(
                "Agent loop iteration %d/%d, stop_reason=%s",
                iteration + 1,
                max_iterations,
                response.stop_reason,
            )

            if response.stop_reason == "end_turn":
                return response

            if response.stop_reason == "tool_use":
                # Append Claude's assistant turn (including tool_use blocks)
                messages.append({
                    "role": "assistant",
                    "content": self._serialise_content(response.content),
                })

                # Execute each tool and collect results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        try:
                            result = self._execute_tool(block.name, block.input)
                        except Exception as exc:
                            logger.warning("Tool %s failed: %s", block.name, exc)
                            result = f"ERROR: {exc}"

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": (
                                json.dumps(result)
                                if isinstance(result, (dict, list))
                                else str(result)
                            ),
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason (e.g. max_tokens) — return as-is
            return response

        raise RuntimeError(
            f"Agent exceeded max tool iterations ({max_iterations}). "
            "This is likely a runaway loop — check your tool implementations."
        )

    def _execute_tool(self, name: str, input: dict) -> Any:
        """Override in subclasses that use tools."""
        raise NotImplementedError(f"Tool '{name}' is not registered on {type(self).__name__}")

    def _call_api(
        self,
        system: Union[str, list[dict]],
        messages: list[dict],
        tools: list[dict],
        max_retries: int = 3,
    ) -> anthropic.types.Message:
        """
        Call the Anthropic API with automatic retry on rate limit (429).
        Uses exponential backoff: 30s, 60s, 120s.

        `system` accepts either:
          - str — a single system prompt (legacy path used by Coach, News, etc.)
          - list[dict] — content blocks with optional cache_control markers
            for prompt caching. Used by TradingAdvisor to cache the stable
            framework prefix while re-rendering the dynamic account snapshot.
        """
        for attempt in range(max_retries):
            try:
                started = time.monotonic()
                response = self.client.messages.create(
                    model=self.MODEL,
                    max_tokens=4096,
                    system=system,
                    messages=messages,
                    tools=tools if tools else anthropic.NOT_GIVEN,
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                self._log_cache_usage(response)
                self._record_call(response, latency_ms)
                return response
            except anthropic.RateLimitError:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                if attempt < max_retries - 1:
                    logger.warning(
                        "Rate limited (attempt %d/%d), waiting %ds...",
                        attempt + 1, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    raise

    def _record_call(
        self,
        response: anthropic.types.Message,
        latency_ms: int,
    ) -> None:
        """
        Persist one row in agent_calls for the observability dashboard.

        Uses a fresh short-lived SQLAlchemy session — NOT self.db — so a
        telemetry insert never gets entangled with whatever transaction
        the calling agent has open. If the insert fails (e.g. the test
        DB doesn't have the table, or the connection is down), we log
        and swallow: telemetry must never break the agent path.

        Token counters are integers in the SDK's response.usage; we
        coerce defensively because mocked test responses sometimes hand
        us MagicMocks instead.
        """
        try:
            from backend.agents.pricing import cost_usd
            from backend.db import SessionLocal
            from backend.db.models import AgentCall

            usage = getattr(response, "usage", None)
            if usage is None:
                return

            def _coerce(value) -> int:
                try:
                    return int(value or 0)
                except (TypeError, ValueError):
                    return 0

            input_tokens = _coerce(getattr(usage, "input_tokens", 0))
            output_tokens = _coerce(getattr(usage, "output_tokens", 0))
            cache_read = _coerce(getattr(usage, "cache_read_input_tokens", 0))
            cache_create = _coerce(getattr(usage, "cache_creation_input_tokens", 0))

            cost = cost_usd(
                model=self.MODEL,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_create,
            )

            session = SessionLocal()
            try:
                session.add(AgentCall(
                    agent=type(self).__name__,
                    model=self.MODEL,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_create,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                ))
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            logger.debug("Agent telemetry write failed (non-fatal): %s", exc)

    @staticmethod
    def _log_cache_usage(response: anthropic.types.Message) -> None:
        """
        Log Anthropic prompt-cache statistics if the response includes them.

        Anthropic returns four input-token counters when caching is in play:
          - input_tokens              : non-cached fresh tokens
          - cache_creation_input_tokens : tokens written to the cache this call
          - cache_read_input_tokens   : tokens read from the cache (cheap!)
          - output_tokens             : completion tokens

        We log these at INFO so the cost win is visible in production logs
        without wading through DEBUG noise. Cache reads cost ~10% of normal
        input tokens; cache writes cost ~125%; non-cached writes cost 100%.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cache_read or cache_create:
            logger.info(
                "Anthropic usage: input=%d, output=%d, cache_create=%d, cache_read=%d",
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                cache_create,
                cache_read,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _serialise_content(content_blocks) -> list[dict]:
        """Convert SDK content-block objects to plain dicts for message history."""
        result = []
        for block in content_blocks:
            if hasattr(block, "model_dump"):
                result.append(block.model_dump())
            else:
                result.append(dict(block))
        return result

    @staticmethod
    def _extract_text(response: anthropic.types.Message) -> str:
        """Return the first TextBlock content from a response."""
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    @staticmethod
    def _extract_json(text: str) -> Any:
        """
        Parse JSON from a Claude response.
        Handles both raw JSON and JSON wrapped in ```json ... ``` code fences.
        """
        # Try code-fenced JSON first
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if match:
            return json.loads(match.group(1))

        # Fall back to parsing the full string
        return json.loads(text)
