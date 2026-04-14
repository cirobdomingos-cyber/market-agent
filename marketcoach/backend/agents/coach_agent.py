"""
CoachAgent — conversational market coach powered by Claude.

The coach is the heart of the UX: a Claude agent that teaches the user about
markets through their own portfolio context. It auto-detects two modes:

  EXPLAIN  -- user asks "what is beta?" or "why did NVDA drop?"
  ANALYSE  -- user asks "should I buy TSLA?" or "review my portfolio risk"

Architecture:
  Uses the same _agentic_loop pattern as NewsAgent. Claude gets two tools
  (web_search + market_data) so it can research before answering. Session
  history (last 20 messages) is loaded for conversation continuity.

Interview angle:
  This demonstrates multi-turn tool-use with persistent memory — the same
  pattern used in production chatbots. The system prompt injection of live
  portfolio/signals data is a form of RAG without a vector store: structured
  context stuffing. Interviewers ask about this as a lightweight alternative
  to full retrieval pipelines.
"""

import json
import logging

import anthropic
from sqlalchemy.orm import Session

from backend.agents.base import AgentResult, BaseAgent
from backend.db import crud
from backend.tools.web_search import WEB_SEARCH_TOOL, execute_web_search
from backend.tools.market_data import MARKET_DATA_TOOL, execute_market_data

logger = logging.getLogger(__name__)

# ── Conversation history window ──────────────────────────────────────────────
HISTORY_WINDOW = 20

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """\
You are an experienced market coach. Your job is NOT to give financial advice — \
it is to build the user's understanding so they can make their own informed decisions.

# Your teaching style
- Explain with concrete examples, historical analogies, and plain language before \
introducing jargon.
- When the user seems confused, slow down. When they're experienced, go deeper.
- Keep responses concise but thorough — no walls of text.
- Connect abstract concepts to the user's actual positions and theses whenever possible.

# Mode detection (auto-detect from the user's message)

EXPLAIN mode (user asks "what is X?" or "why did Y happen?"):
  1. Give a clear, layered explanation (simple first, then nuance).
  2. Offer to connect the concept to their portfolio.
  3. Use web_search if the question is about recent events.

ANALYSE mode (user asks "should I do X?" or "review my portfolio"):
  1. Present the bull case AND the bear case with equal weight.
  2. Cite actual signals and theses from context when relevant.
  3. Use the market_data tool to fetch current prices, technicals, or fundamentals \
for any specific tickers under discussion.
  4. Use web_search for recent news if needed.
  5. NEVER give a direct buy/sell recommendation.
  6. End with: "Key questions to answer before deciding: ..." (2-4 bullet points).

# Tools
- web_search: look up current news and recent events.
- market_data: get real-time prices, technical indicators, and fundamental data for tickers.

Always prefer using tools over guessing at current prices or recent events.

# User context (live data)

Portfolio positions:
{portfolio}

Recent investment theses (open):
{theses}

Recent market signals (last 4h):
{signals}

Prediction accuracy history:
{accuracy}
"""


def _format_portfolio(portfolio: list[dict]) -> str:
    """Format portfolio positions for the system prompt."""
    if not portfolio:
        return "No positions currently held."
    lines = []
    for pos in portfolio:
        ticker = pos.get("ticker", pos.get("symbol", "???"))
        qty = pos.get("qty", pos.get("quantity", "?"))
        market_value = pos.get("market_value", "")
        unrealized_pl = pos.get("unrealized_pl", pos.get("unrealized_pnl", ""))
        line = f"  {ticker}: {qty} shares"
        if market_value:
            line += f", value ${market_value}"
        if unrealized_pl:
            line += f", P&L ${unrealized_pl}"
        lines.append(line)
    return "\n".join(lines)


def _format_theses(theses: list[dict]) -> str:
    """Format open theses for the system prompt."""
    if not theses:
        return "No open theses."
    lines = []
    for t in theses[:10]:  # cap at 10 to avoid token bloat
        ticker = t.get("ticker", "?")
        direction = t.get("direction", "?")
        confidence = t.get("confidence", "?")
        summary = t.get("summary", t.get("rationale", ""))[:120]
        lines.append(f"  {ticker} ({direction}, confidence {confidence}): {summary}")
    return "\n".join(lines)


def _format_signals(signals: list[dict]) -> str:
    """Format recent signals for the system prompt."""
    if not signals:
        return "No recent signals."
    lines = []
    for s in signals[:10]:
        ticker = s.get("ticker", "?")
        sentiment = s.get("sentiment", "?")
        headline = s.get("headline", "")[:100]
        lines.append(f"  {ticker} [{sentiment}]: {headline}")
    return "\n".join(lines)


def _format_accuracy(accuracy: dict) -> str:
    """Format accuracy stats for the system prompt."""
    if not accuracy:
        return "No accuracy data yet."
    total = accuracy.get("total", 0)
    correct = accuracy.get("correct", 0)
    rate = accuracy.get("accuracy_rate", accuracy.get("rate", 0))
    return f"  {correct}/{total} correct ({rate:.0%} accuracy)" if total else "No resolved theses yet."


# ── Agent ────────────────────────────────────────────────────────────────────

class CoachAgent(BaseAgent):
    """Conversational coach with persistent session history and tool access."""

    def __init__(self, db: Session, client: anthropic.Anthropic):
        super().__init__(db, client)

    def run(self, context: dict) -> AgentResult:
        """
        Run a full coaching turn: load history, call Claude with tools, persist result.

        context keys:
          session_id:    str
          user_message:  str
          portfolio:     list[dict]  (from Alpaca)
          theses:        list[dict]  (recent open theses)
          signals:       list[dict]  (last 4h signals summary)
          accuracy:      dict        (thesis accuracy stats)
        """
        session_id = context.get("session_id", "default")
        user_message = context.get("user_message", "")

        # 1. Persist the user message
        crud.create_message(self.db, session_id=session_id, role="user", content=user_message)

        try:
            # 2. Build system prompt with live context
            system = SYSTEM_PROMPT_TEMPLATE.format(
                portfolio=_format_portfolio(context.get("portfolio", [])),
                theses=_format_theses(context.get("theses", [])),
                signals=_format_signals(context.get("signals", [])),
                accuracy=_format_accuracy(context.get("accuracy", {})),
            )

            # 3. Load conversation history for continuity
            messages = self._build_messages(session_id, user_message)

            # 4. Run the agentic loop with both tools
            response = self._agentic_loop(
                system=system,
                messages=messages,
                tools=[WEB_SEARCH_TOOL, MARKET_DATA_TOOL],
            )

            reply = self._extract_text(response)

            # 5. Persist assistant reply
            crud.create_message(self.db, session_id=session_id, role="assistant", content=reply)

            logger.info(
                "CoachAgent: session=%s, reply_length=%d chars",
                session_id,
                len(reply),
            )

            return AgentResult(
                success=True,
                data={
                    "session_id": session_id,
                    "reply": reply,
                },
            )

        except Exception as exc:
            logger.exception("CoachAgent failed for session %s", session_id)
            error_reply = (
                "I ran into an issue processing your question. "
                "Could you try rephrasing, or ask me something else?"
            )
            crud.create_message(
                self.db, session_id=session_id, role="assistant", content=error_reply
            )
            return AgentResult(
                success=False,
                data={"session_id": session_id, "reply": error_reply},
                error=str(exc),
            )

    # ── Message history ──────────────────────────────────────────────────────

    def _build_messages(self, session_id: str, current_message: str) -> list[dict]:
        """
        Load the last HISTORY_WINDOW messages from the DB and append the
        current user message. Returns the messages list for the Claude API.

        We only include user/assistant turns (no tool_use/tool_result from
        previous sessions) since those are internal to past agentic loops.
        """
        history = crud.get_session_messages(self.db, session_id)

        # Take the last HISTORY_WINDOW messages (excluding the one we just persisted)
        # The current message was already persisted above, so it's the last in the list.
        # We want prior messages for context, then append the current one fresh.
        prior = history[:-1] if history else []  # exclude the just-persisted user msg
        prior = prior[-HISTORY_WINDOW:]

        messages: list[dict] = []
        for msg in prior:
            messages.append({"role": msg.role, "content": msg.content})

        # Append the current user turn
        messages.append({"role": "user", "content": current_message})
        return messages

    # ── Tool dispatch ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, input: dict):
        """Route tool calls to the appropriate executor."""
        if name == "web_search":
            return execute_web_search(
                query=input["query"],
                max_results=input.get("max_results", 5),
                search_type=input.get("search_type", "news"),
            )
        if name == "market_data":
            return execute_market_data(**input)
        return super()._execute_tool(name, input)
