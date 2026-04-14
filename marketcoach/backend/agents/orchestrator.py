"""
Orchestrator — composes agents into named pipelines.

Pipelines:
  "intelligence"  — NewsAgent -> AnalysisAgent -> thesis resolution (the scheduled 4h run)
  "coach"         — CoachAgent (single turn, stateful via DB)
  "memory"        — MemoryAgent (extract user profile from conversation)
  "full"          — intelligence + memory extraction

The orchestrator owns the Anthropic client and Alpaca client, injecting them
into agents as needed. This keeps agent classes clean of config concerns.
"""

import logging
from typing import Any, Optional

import anthropic
from sqlalchemy.orm import Session

from backend.agents.analysis_agent import AnalysisAgent
from backend.agents.coach_agent import CoachAgent
from backend.agents.memory_agent import MemoryAgent
from backend.agents.news_agent import NewsAgent
from backend.agents.trade_idea_agent import TradeIdeaAgent
from backend.agents.trading_advisor_agent import TradingAdvisorAgent
from backend.agents.base import AgentResult
from backend.config import settings
from backend.db import crud
from backend.brokers import get_broker

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # -- Pipelines -------------------------------------------------------------

    def run_intelligence_pipeline(self, context: dict | None = None) -> dict[str, AgentResult]:
        """
        The core scheduled pipeline: fetch news -> generate theses -> resolve expired.
        Runs every 4 hours via APScheduler.
        """
        context = context or {"watchlist": settings.watchlist}
        results: dict[str, AgentResult] = {}

        logger.info("Orchestrator: starting intelligence pipeline")

        # Step 1 — fetch and extract news signals
        news_agent = NewsAgent(
            self.db,
            self.client,
            cache_ttl_minutes=settings.news_cache_ttl_minutes,
        )
        news_result = news_agent.run(context)
        results["news"] = news_result

        if not news_result.success:
            logger.warning("News agent failed — skipping analysis: %s", news_result.error)
            return results

        # Step 2 — generate theses from signals
        analysis_agent = AnalysisAgent(self.db, self.client)
        analysis_result = analysis_agent.run(context)
        results["analysis"] = analysis_result

        # Step 3 — resolve any expired theses against actual prices
        resolved_count = analysis_agent.resolve_expired_theses()
        results["resolution"] = AgentResult(
            success=True,
            data={"resolved": resolved_count},
        )

        # Step 4 — generate trade ideas from theses
        if analysis_result.success and analysis_result.data.get("theses"):
            # Get portfolio value from Alpaca if available
            portfolio_value = 100_000.0  # default
            broker = get_broker()
            existing_positions = []
            if broker:
                account = broker.get_account()
                if "portfolio_value" in account:
                    portfolio_value = account["portfolio_value"]
                existing_positions = broker.get_positions()

            # Get user risk tolerance from memories
            risk_tolerance = "moderate"
            risk_mem = crud.get_memory(self.db, "risk_tolerance")
            if risk_mem:
                val = risk_mem.value.lower()
                if val in ("conservative", "moderate", "aggressive"):
                    risk_tolerance = val

            trade_agent = TradeIdeaAgent(
                self.db, self.client, portfolio_value=portfolio_value,
            )
            trade_result = trade_agent.run({
                "theses": analysis_result.data["theses"],
                "portfolio_value": portfolio_value,
                "risk_tolerance": risk_tolerance,
                "existing_positions": existing_positions,
            })
            results["trade_ideas"] = trade_result
        else:
            results["trade_ideas"] = AgentResult(
                success=True,
                data={"trade_ideas": [], "skipped": [], "reason": "no theses to process"},
            )

        # Step 5 — auto-generate news reactions for high-impact signals
        reactions_created = self._trigger_news_reactions(
            news_result.data.get("signals", []) if news_result.success else []
        )
        results["news_reactions"] = AgentResult(
            success=True,
            data={"created": reactions_created},
        )

        logger.info(
            "Orchestrator: intelligence pipeline complete. "
            "signals=%s theses=%s resolved=%s trade_ideas=%s reactions=%s",
            len(news_result.data.get("signals", [])) if news_result.success else 0,
            len(analysis_result.data.get("theses", [])) if analysis_result.success else 0,
            resolved_count,
            len(results["trade_ideas"].data.get("trade_ideas", [])),
            reactions_created,
        )
        return results

    # ── News reactions ───────────────────────────────────────────────────────

    _TRIGGER_PRIORITY = {"position": 0, "thesis": 1, "watchlist": 2}

    def _classify_reaction_trigger(
        self,
        ticker: str,
        position_tickers: set[str],
        thesis_tickers: set[str],
        watchlist_tickers: set[str],
    ) -> Optional[str]:
        """Return the highest-priority trigger reason for this ticker, or None
        if it doesn't match any user-relevant set (and should be skipped)."""
        if ticker in position_tickers:
            return "position"
        if ticker in thesis_tickers:
            return "thesis"
        if ticker in watchlist_tickers:
            return "watchlist"
        return None

    def _trigger_news_reactions(self, signals: list[dict]) -> int:
        """
        Filter signals for high-impact events, dedupe, rate-limit, and
        auto-generate TradingAdvisor reactions. Returns number of reactions
        created (successful + failed).

        The filter has three gates:
          1. Impact: confidence >= threshold AND sentiment is directional
          2. Relevance: ticker affects a position, open thesis, or watchlist
          3. Dedupe: no existing reaction for this ticker in the last N hours
        Then sorted by (relevance priority, confidence desc) and capped by
        settings.news_reactions_per_run_max to prevent runaway spend.
        """
        if not settings.news_reactions_enabled or not signals:
            return 0

        # Gate 1 — impact filter
        impactful = [
            s for s in signals
            if s.get("confidence", 0) >= settings.news_reaction_min_confidence
            and s.get("sentiment") in ("bullish", "bearish")
        ]
        if not impactful:
            logger.info(
                "News reactions: 0 impactful signals (threshold=%.2f)",
                settings.news_reaction_min_confidence,
            )
            return 0

        # Gate 2 — relevance classification
        broker = get_broker()
        position_tickers: set[str] = set()
        if broker:
            position_tickers = {
                (p.get("ticker") or "").upper() for p in broker.get_positions()
            }
        thesis_tickers = {
            (t.ticker or "").upper() for t in crud.get_open_theses(self.db, limit=100)
        }
        watchlist_tickers = {t.upper() for t in settings.watchlist}

        candidates: list[tuple[dict, str, str]] = []
        for sig in impactful:
            ticker = (sig.get("ticker") or "").upper()
            if not ticker:
                continue
            reason = self._classify_reaction_trigger(
                ticker, position_tickers, thesis_tickers, watchlist_tickers
            )
            if reason is None:
                continue
            # Gate 3 — dedupe window
            if crud.has_recent_reaction_for_ticker(
                self.db, ticker, settings.news_reaction_dedupe_hours
            ):
                logger.debug("News reactions: dedupe skip for %s", ticker)
                continue
            candidates.append((sig, ticker, reason))

        if not candidates:
            return 0

        # Sort by (priority, -confidence) and cap
        candidates.sort(
            key=lambda c: (
                self._TRIGGER_PRIORITY[c[2]],
                -float(c[0].get("confidence", 0)),
            )
        )
        candidates = candidates[: settings.news_reactions_per_run_max]

        created = 0
        for sig, ticker, reason in candidates:
            headline = sig.get("headline", "")
            reaction_prompt = (
                f"React to this news on {ticker}: {headline}\n\n"
                "Assess impact on any open positions and call out new "
                "opportunities this creates. Be specific about urgency — what "
                "needs action before next market open?"
            )
            try:
                advisor_result = self.run_advisor({
                    "session_id": settings.news_reaction_session_id,
                    "user_message": reaction_prompt,
                })
                content = (
                    advisor_result.data.get("reply", "")
                    if advisor_result.data
                    else ""
                )
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=headline,
                    content=content,
                    trigger_reason=reason,
                    status="unread" if advisor_result.success else "failed",
                    error=advisor_result.error,
                )
                created += 1
            except Exception as exc:
                logger.warning(
                    "News reaction generation failed for %s: %s", ticker, exc
                )
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=headline,
                    content="",
                    trigger_reason=reason,
                    status="failed",
                    error=str(exc),
                )
                created += 1

        logger.info("News reactions: created %d reactions", created)
        return created

    def run_coach(self, context: dict) -> AgentResult:
        """
        Single coach turn. Context must include session_id and user_message.
        Enriches context with Alpaca portfolio data if available.
        """
        # Inject live portfolio from Alpaca if configured
        broker = get_broker()
        if broker:
            context.setdefault("portfolio", broker.get_positions())

        coach = CoachAgent(self.db, self.client)
        result = coach.run(context)

        # Fire-and-forget memory extraction from this conversation
        # This runs after every coach turn to keep user profile up to date
        try:
            session_id = context.get("session_id", "default")
            messages = crud.get_session_messages(self.db, session_id)
            if len(messages) >= 4:  # only extract after a meaningful exchange
                recent = [{"role": m.role, "content": m.content} for m in messages[-10:]]
                memory_agent = MemoryAgent(self.db, self.client)
                memory_result = memory_agent.run({
                    "action": "extract",
                    "session_id": session_id,
                    "messages": recent,
                })
                if memory_result.success and memory_result.data.get("extracted", 0) > 0:
                    logger.info(
                        "MemoryAgent extracted %d memories from session %s",
                        memory_result.data["extracted"],
                        session_id,
                    )
        except Exception as exc:
            # Memory extraction is best-effort — never block the coach response
            logger.warning("Memory extraction failed (non-blocking): %s", exc)

        return result

    def run_advisor(self, context: dict) -> AgentResult:
        """
        Single trading-advisor turn. Context must include session_id and
        user_message. Enriches context with live Alpaca account data and
        positions so the prompt can ground position sizing in real numbers.
        """
        broker = get_broker()
        if broker:
            context.setdefault("account", broker.get_account())
            context.setdefault("positions", broker.get_positions())
        else:
            context.setdefault("account", {})
            context.setdefault("positions", [])

        advisor = TradingAdvisorAgent(self.db, self.client)
        return advisor.run(context)

    def run_weekly_plan(self, trigger: str = "scheduled") -> AgentResult:
        """
        Generate and persist a weekly trading plan via the TradingAdvisorAgent.

        This is the entry point for both the Sunday-night cron job and the
        manual "run now" button in the UI. The generated plan is stored in the
        weekly_plans table so the frontend can display the latest one without
        re-invoking Claude.
        """
        from backend.config import settings as _settings

        session_id = _settings.weekly_plan_session_id

        theses = crud.get_open_theses(self.db, limit=10)
        signals = crud.get_recent_signals(self.db, hours=168)  # last 7 days

        context = {
            "session_id": session_id,
            "user_message": (
                "Weekly plan. Run Layer 1–2 macro + sector analysis, list this "
                "week's key events (earnings, Fed, econ data), and give specific "
                "trades to watch with entry levels. Include management notes for "
                "any existing positions."
            ),
            "theses": [
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "confidence": t.confidence,
                    "timeframe": t.timeframe,
                    "reasoning": (t.reasoning or "")[:200],
                }
                for t in theses
            ],
            "signals": [
                {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
                for s in signals[:20]
            ],
        }

        result = self.run_advisor(context)

        reply = result.data.get("reply", "") if result.data else ""
        crud.create_weekly_plan(
            self.db,
            session_id=session_id,
            content=reply,
            status="completed" if result.success else "failed",
            error=result.error,
            trigger=trigger,
        )

        logger.info(
            "Weekly plan generated (trigger=%s, success=%s, length=%d chars)",
            trigger,
            result.success,
            len(reply),
        )
        return result

    def run_morning_brief(self, trigger: str = "scheduled") -> AgentResult:
        """
        Generate and persist a daily pre-market brief via the TradingAdvisor.

        Tighter, shorter, and more time-sensitive than the weekly plan.
        Designed to be read on a phone in bed before market open.
        """
        from backend.config import settings as _settings

        session_id = _settings.morning_brief_session_id

        theses = crud.get_open_theses(self.db, limit=10)
        signals = crud.get_recent_signals(self.db, hours=24)  # last 24h

        # The advisor knows about its modes — give it a focused user message
        # rather than the canonical "react to" or "weekly plan" trigger so the
        # output stays short and pre-market-oriented instead of running the
        # full Layer 1–4 framework.
        user_message = (
            "Generate today's morning brief. Keep it SHORT and scannable — "
            "I'm reading this on my phone before the open. Use these sections "
            "with bullet points (no long paragraphs):\n\n"
            "1. **Overnight** — Asia/Europe close, US futures direction, any "
            "macro shock from the last 12 hours.\n"
            "2. **Earnings today** — who reports premarket vs after the bell. "
            "Flag any in my positions or watchlist.\n"
            "3. **Premarket movers** — any of my positions or watchlist tickers "
            "gapping up/down and why.\n"
            "4. **Key events today** — Fed speakers, economic data releases, "
            "FDA decisions, anything that could move the tape.\n"
            "5. **Action items** — specific things to watch for at the open. "
            "Be concrete: 'NVDA: watch for hold above $450, stop $442'. If "
            "there's nothing to do, say 'No actions — sit on hands today'.\n\n"
            "Skip Layer 1–2 framework boilerplate. Skip the 'risk reminder' "
            "footer. Get straight to what I need to know."
        )

        context = {
            "session_id": session_id,
            "user_message": user_message,
            "theses": [
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "confidence": t.confidence,
                    "timeframe": t.timeframe,
                    "reasoning": (t.reasoning or "")[:200],
                }
                for t in theses
            ],
            "signals": [
                {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
                for s in signals[:15]
            ],
        }

        result = self.run_advisor(context)

        reply = result.data.get("reply", "") if result.data else ""
        crud.create_morning_brief(
            self.db,
            session_id=session_id,
            content=reply,
            status="completed" if result.success else "failed",
            error=result.error,
            trigger=trigger,
        )

        logger.info(
            "Morning brief generated (trigger=%s, success=%s, length=%d chars)",
            trigger,
            result.success,
            len(reply),
        )
        return result

    def get_user_profile(self) -> dict:
        """Return the user's stored memories/preferences."""
        memory_agent = MemoryAgent(self.db, self.client)
        result = memory_agent.run({"action": "read"})
        return result.data if result.success else {}

    def get_pipeline_status(self, results: dict[str, AgentResult]) -> dict[str, Any]:
        """Summarise pipeline results for the API response."""
        return {
            agent: {
                "success": r.success,
                "error": r.error,
                "run_at": r.run_at.isoformat() if r.run_at else None,
            }
            for agent, r in results.items()
        }
