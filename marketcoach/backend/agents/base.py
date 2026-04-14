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
from typing import Any, Optional

import anthropic
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Keep this in one place — easy to swap model versions.
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
    """

    def __init__(self, db: Session, client: anthropic.Anthropic):
        self.db = db
        self.client = client

    @abstractmethod
    def run(self, context: dict) -> AgentResult:
        raise NotImplementedError

    # ── Agentic loop ──────────────────────────────────────────────────────────

    def _agentic_loop(
        self,
        system: str,
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
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_retries: int = 3,
    ) -> anthropic.types.Message:
        """
        Call the Anthropic API with automatic retry on rate limit (429).
        Uses exponential backoff: 30s, 60s, 120s.
        """
        for attempt in range(max_retries):
            try:
                return self.client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=system,
                    messages=messages,
                    tools=tools if tools else anthropic.NOT_GIVEN,
                )
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
