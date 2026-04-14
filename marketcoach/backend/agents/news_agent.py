"""
NewsAgent — Phase 2

Responsibilities:
  1. Run targeted web searches for financial news (macro + watchlist tickers)
  2. Ask Claude to extract structured signals from those results
  3. Persist Signal rows to the database
  4. Cache results for NEWS_CACHE_TTL_MINUTES to avoid redundant API calls

Architecture note (interview angle):
  This agent uses the 'tool-use agentic loop' pattern: we give Claude a
  web_search tool, let it decide what to search and how many times, then
  ask it to synthesise results into structured JSON. This decouples the
  search strategy from our code — Claude figures out which queries to run.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.base import AgentResult, BaseAgent
from backend.db import crud
from backend.tools.web_search import WEB_SEARCH_TOOL, execute_web_search

logger = logging.getLogger(__name__)

# ── Output schema ─────────────────────────────────────────────────────────────

class RawSignal(BaseModel):
    ticker: str
    sentiment: str   # bullish | bearish | neutral
    confidence: float
    source: str
    headline: str
    raw_text: str


class NewsAgentOutput(BaseModel):
    signals: list[RawSignal]
    summary: str
    searched_at: datetime


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a financial news analyst extracting structured market signals.

Your job:
1. Use the web_search tool to gather recent financial news (last 24 hours when possible)
2. Search for: top market headlines, Federal Reserve / central bank news, earnings reports,
   and specific news for each ticker in the watchlist
3. From the gathered news, extract structured signals

After completing all searches, output ONLY a JSON object in this exact format:
```json
{
  "signals": [
    {
      "ticker": "NVDA",
      "sentiment": "bullish",
      "confidence": 0.85,
      "source": "Reuters",
      "headline": "NVIDIA Q4 revenue beats estimates by 12%",
      "raw_text": "Brief summary of the article..."
    }
  ],
  "summary": "One-paragraph market summary of the most important developments"
}
```

Rules:
- sentiment must be exactly: bullish, bearish, or neutral
- confidence is 0.0–1.0: use 0.9+ only for major unambiguous news, 0.5–0.7 for mixed signals
- Include only tickers from the provided watchlist unless a major macro event affects the whole market
- Use SPY or QQQ for broad market signals
- Do not include tickers you found no meaningful news for
"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class NewsAgent(BaseAgent):
    """
    Fetches financial news via web search and extracts sentiment signals.
    Results are cached for NEWS_CACHE_TTL_MINUTES to respect rate limits.
    """

    _cache: dict = {}  # class-level cache shared across instances

    def __init__(
        self,
        db: Session,
        client: anthropic.Anthropic,
        cache_ttl_minutes: int = 30,
    ):
        super().__init__(db, client)
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)

    def run(self, context: dict) -> AgentResult:
        cache_key = "news_signals"
        cached = self._get_cache(cache_key)
        if cached is not None:
            logger.info("NewsAgent: returning cached signals (age < %s)", self.cache_ttl)
            return AgentResult(success=True, data=cached)

        watchlist: list[str] = context.get("watchlist", ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"])

        try:
            output = self._fetch_and_extract(watchlist)
            stored = self._store_signals(output.signals)
            self._set_cache(cache_key, output.model_dump(mode="json"))

            logger.info(
                "NewsAgent: extracted %d signals, stored %d new",
                len(output.signals),
                stored,
            )
            return AgentResult(success=True, data=output.model_dump(mode="json"))

        except Exception as exc:
            logger.exception("NewsAgent failed")
            return AgentResult(success=False, data={}, error=str(exc))

    # ── Core logic ────────────────────────────────────────────────────────────

    def _fetch_and_extract(self, watchlist: list[str]) -> NewsAgentOutput:
        watchlist_str = ", ".join(watchlist)
        user_prompt = (
            f"Today's watchlist: {watchlist_str}\n\n"
            "Search for recent financial news covering:\n"
            "1. Top 10 financial headlines right now\n"
            "2. Federal Reserve / central bank announcements\n"
            "3. Earnings reports or guidance for any watchlist tickers\n"
            "4. Sector news relevant to the watchlist\n\n"
            "Run as many searches as you need, then output the structured JSON."
        )

        messages = [{"role": "user", "content": user_prompt}]
        tools = [WEB_SEARCH_TOOL]

        response = self._agentic_loop(
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
        )

        text = self._extract_text(response)
        raw = self._extract_json(text)

        signals = [RawSignal(**s) for s in raw["signals"]]
        return NewsAgentOutput(
            signals=signals,
            summary=raw.get("summary", ""),
            searched_at=datetime.now(timezone.utc),
        )

    def _execute_tool(self, name: str, input: dict):
        if name == "web_search":
            return execute_web_search(
                query=input["query"],
                max_results=input.get("max_results", 5),
                search_type=input.get("search_type", "news"),
            )
        return super()._execute_tool(name, input)

    # ── DB persistence ────────────────────────────────────────────────────────

    def _store_signals(self, signals: list[RawSignal]) -> int:
        """Persist signals to DB in a single transaction. Returns count inserted."""
        return crud.create_signals_batch(
            self.db,
            [
                dict(
                    ticker=s.ticker.upper(),
                    sentiment=s.sentiment,
                    confidence=s.confidence,
                    source=s.source,
                    headline=s.headline,
                    raw_text=s.raw_text,
                )
                for s in signals
            ],
        )

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _get_cache(self, key: str) -> Optional[dict]:
        entry = NewsAgent._cache.get(key)
        if entry is None:
            return None
        if datetime.now(timezone.utc) - entry["ts"] > self.cache_ttl:
            del NewsAgent._cache[key]
            return None
        return entry["data"]

    def _set_cache(self, key: str, data: dict) -> None:
        NewsAgent._cache[key] = {"ts": datetime.now(timezone.utc), "data": data}
