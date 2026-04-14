"""
MemoryAgent — extracts and persists user profile information from conversations.

Uses Claude to analyze recent conversation transcripts and identify persistent
user context: risk tolerance, investment goals, sectors of interest, time horizon,
experience level, and notable observations.

This is distinct from session chat history (stored in ChatMessage).
Memory is more durable signal about who the user is and what they've learned.
"""

import json
import logging
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from backend.agents.base import MODEL, AgentResult, BaseAgent
from backend.db.crud import get_all_memories, upsert_memory

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM = """\
You are a memory extraction agent for a personal investment coach.
Your job is to analyze a conversation transcript and extract any persistent
user profile information worth remembering across sessions.

Extract ONLY information the user explicitly stated or strongly implied.
Do NOT infer preferences the user did not express.

Categories and example keys:
  profile     — risk_tolerance, experience_level, investment_budget, time_horizon
  preference  — sector_interest, preferred_asset_class, trading_style, avoided_sectors
  observation — market_observation, lesson_learned, thesis_outcome_note

Respond with ONLY a JSON object (no markdown fences) in this format:
{
  "memories": [
    {"key": "risk_tolerance", "value": "moderate — prefers diversified ETFs over individual stocks", "category": "profile"},
    {"key": "sector_interest", "value": "bullish on semiconductors and AI infrastructure", "category": "preference"}
  ]
}

If there is nothing worth extracting, return: {"memories": []}
"""


class MemoryAgent(BaseAgent):
    """Extracts and persists user profile context from conversations."""

    def __init__(self, db: Session, client: anthropic.Anthropic):
        super().__init__(db, client)

    def run(self, context: dict) -> AgentResult:
        """
        context keys:
          action: "extract" | "read"
          session_id: str          (required for extract)
          messages: list[dict]     (recent conversation, required for extract)
        """
        action = context.get("action", "read")

        if action == "read":
            return self._read_memories()

        if action == "extract":
            return self._extract_memories(context)

        return AgentResult(
            success=False,
            data={},
            error=f"Unknown action: {action}",
        )

    # ── Read all memories ────────────────────────────────────────────────────

    def _read_memories(self) -> AgentResult:
        """Return all stored memories as a dict keyed by memory key."""
        memories = get_all_memories(self.db)
        data = {
            m.key: {"value": m.value, "category": m.category}
            for m in memories
        }
        return AgentResult(success=True, data=data)

    # ── Extract memories from conversation ───────────────────────────────────

    def _extract_memories(self, context: dict) -> AgentResult:
        """Use Claude to analyze a conversation and persist extracted memories."""
        messages = context.get("messages", [])
        session_id = context.get("session_id", "unknown")

        if not messages:
            return AgentResult(
                success=True,
                data={"extracted": 0, "session_id": session_id},
            )

        # Build the transcript for Claude to analyze
        transcript = self._format_transcript(messages)

        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=EXTRACTION_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this conversation and extract any user profile "
                            f"information worth remembering:\n\n{transcript}"
                        ),
                    }
                ],
            )

            raw_text = self._extract_text(response)
            parsed = self._extract_json(raw_text)
            extracted = parsed.get("memories", [])

        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("MemoryAgent failed to parse extraction result: %s", exc)
            return AgentResult(
                success=False,
                data={"session_id": session_id},
                error=f"Failed to parse Claude response: {exc}",
            )
        except anthropic.APIError as exc:
            logger.error("MemoryAgent API call failed: %s", exc)
            return AgentResult(
                success=False,
                data={"session_id": session_id},
                error=f"Claude API error: {exc}",
            )

        # Persist each extracted memory
        persisted = []
        for mem in extracted:
            key = mem.get("key")
            value = mem.get("value")
            category = mem.get("category", "general")
            if key and value:
                upsert_memory(self.db, key=key, value=value, category=category)
                persisted.append({"key": key, "value": value, "category": category})
                logger.info("MemoryAgent persisted: %s = %s", key, value[:80])

        return AgentResult(
            success=True,
            data={
                "session_id": session_id,
                "extracted": len(persisted),
                "memories": persisted,
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _format_transcript(messages: list[dict]) -> str:
        """Convert message list to a readable transcript string."""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
