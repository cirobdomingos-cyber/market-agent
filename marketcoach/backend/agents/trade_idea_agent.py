"""
TradeIdeaAgent -- Phase 3

Converts AnalysisAgent theses into actionable trade ideas by combining:
  1. Live market data (price, technicals) via the market_data tool
  2. Position sizing and risk levels via risk.py calculations
  3. Plain-language rationale from Claude
  4. DB persistence for downstream execution/display

Interview angle:
  This agent demonstrates the "enrich + validate + persist" pipeline pattern
  common in production trading systems. Each thesis goes through a gate
  (portfolio_risk_check) before becoming a trade idea -- the same approve/reject
  pattern used in credit decisioning, ad serving, and order management systems.
  Interviewers ask about this because it shows you think about guardrails, not
  just happy-path generation.

  The single-prompt batched rationale call is a cost optimisation: one Claude
  call for N ideas instead of N calls. At scale, batching LLM calls is the
  difference between a viable product and a billing nightmare.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import anthropic
from sqlalchemy.orm import Session

from backend.agents.base import AgentResult, BaseAgent
from backend.db import crud
from backend.tools.market_data import execute_market_data
from backend.tools.risk import (
    calculate_position_size,
    calculate_risk_reward_ratio,
    calculate_stop_loss,
    calculate_take_profit,
    classify_horizon,
    portfolio_risk_check,
)

logger = logging.getLogger(__name__)

# ── Expiry mapping ────────────────────────────────────────────────────────────

_HORIZON_EXPIRY_DAYS = {
    "short": 5,
    "medium": 21,
    "long": 60,
}

# ── Rationale system prompt ──────────────────────────────────────────────────

RATIONALE_SYSTEM_PROMPT = (
    "You are a trade idea analyst. For each trade, write a concise 2-3 sentence "
    "rationale explaining why this entry/stop/target makes sense given the thesis "
    "and current technicals. Be specific about price levels and catalysts. Do NOT "
    'say "buy" or "sell" -- frame as "the thesis suggests" or "if the thesis '
    'plays out".'
)


class TradeIdeaAgent(BaseAgent):
    """Generates actionable trade ideas from theses + market data + risk calculations."""

    def __init__(
        self,
        db: Session,
        client: anthropic.Anthropic,
        portfolio_value: float = 100_000.0,
    ):
        super().__init__(db, client)
        self.portfolio_value = portfolio_value

    def run(self, context: dict) -> AgentResult:
        """
        Generate trade ideas from theses.

        context keys:
          theses: list[dict]          -- from AnalysisAgent (ticker, direction,
                                         confidence, timeframe, reasoning, key_risks)
          portfolio_value: float      -- from Alpaca account or default
          risk_tolerance: str         -- from user memories or default "moderate"
          existing_positions: list[dict] -- current Alpaca positions
        """
        theses = context.get("theses", [])
        portfolio_value = context.get("portfolio_value", self.portfolio_value)
        risk_tolerance = context.get("risk_tolerance", "moderate")
        existing_positions = context.get("existing_positions", [])

        if not theses:
            logger.info("TradeIdeaAgent: no theses provided, skipping")
            return AgentResult(success=True, data={"trade_ideas": [], "skipped": []})

        # 1. Expire stale ideas before generating new ones
        expired_count = crud.expire_stale_trade_ideas(self.db)
        if expired_count:
            logger.info("TradeIdeaAgent: expired %d stale trade ideas", expired_count)

        try:
            # 2. Build raw ideas from theses + market data + risk calcs
            raw_ideas, skipped = self._build_raw_ideas(
                theses=theses,
                portfolio_value=portfolio_value,
                risk_tolerance=risk_tolerance,
                existing_positions=existing_positions,
            )

            if not raw_ideas:
                logger.info(
                    "TradeIdeaAgent: all %d theses were skipped (no viable ideas)",
                    len(theses),
                )
                return AgentResult(
                    success=True,
                    data={"trade_ideas": [], "skipped": skipped},
                )

            # 3. Generate rationales via Claude (single batched call)
            raw_ideas = self._generate_rationales(raw_ideas)

            # 4. Persist to DB
            stored = crud.create_trade_ideas_batch(self.db, raw_ideas)
            logger.info(
                "TradeIdeaAgent: generated %d trade ideas from %d theses "
                "(%d skipped), stored %d",
                len(raw_ideas),
                len(theses),
                len(skipped),
                stored,
            )

            return AgentResult(
                success=True,
                data={
                    "trade_ideas": raw_ideas,
                    "skipped": skipped,
                    "stored_count": stored,
                    "expired_count": expired_count,
                },
            )

        except Exception as exc:
            logger.exception("TradeIdeaAgent failed")
            return AgentResult(success=False, data={}, error=str(exc))

    # ── Core pipeline ────────────────────────────────────────────────────────

    def _build_raw_ideas(
        self,
        theses: list[dict],
        portfolio_value: float,
        risk_tolerance: str,
        existing_positions: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """
        For each thesis, fetch market data, compute risk levels, and build
        a trade idea dict. Returns (ideas, skipped) where skipped contains
        tickers that were dropped and the reason why.
        """
        ideas: list[dict] = []
        skipped: list[dict] = []

        for thesis in theses:
            ticker = thesis.get("ticker", "").upper()
            if not ticker:
                logger.warning("TradeIdeaAgent: thesis missing ticker, skipping")
                skipped.append({"ticker": "UNKNOWN", "reason": "missing ticker"})
                continue

            # a. Fetch quote + technicals
            quote = self._fetch_market_data(ticker, "quote")
            technicals = self._fetch_market_data(ticker, "technicals")

            if quote is None or technicals is None:
                skipped.append({"ticker": ticker, "reason": "market data unavailable"})
                continue

            current_price = quote.get("price")
            if not current_price or current_price <= 0:
                logger.warning(
                    "TradeIdeaAgent: invalid price for %s: %s", ticker, current_price
                )
                skipped.append({"ticker": ticker, "reason": "invalid price"})
                continue

            # b. Classify horizon from thesis timeframe
            direction = thesis.get("direction", "neutral").lower()
            confidence = thesis.get("confidence", 0.5)
            timeframe = thesis.get("timeframe", "3-10 days")
            horizon = classify_horizon(timeframe)

            # Map thesis direction to trade direction
            if direction == "bullish":
                trade_direction = "long"
                strategy = "equity_long"
            elif direction == "bearish":
                trade_direction = "short"
                strategy = "equity_short"
            else:
                # Neutral theses don't produce trade ideas
                logger.debug(
                    "TradeIdeaAgent: skipping neutral thesis for %s", ticker
                )
                skipped.append({"ticker": ticker, "reason": "neutral direction"})
                continue

            # c. Calculate stop-loss, take-profit, position size
            atr = technicals.get("atr_14")
            # Use 52-week levels as support/resistance
            support = technicals.get("week_52_low")
            resistance = technicals.get("week_52_high")

            stop_loss = calculate_stop_loss(
                entry_price=current_price,
                direction=trade_direction,
                atr=atr,
                support_level=support,
                resistance_level=resistance,
            )
            take_profit = calculate_take_profit(
                entry_price=current_price,
                stop_loss=stop_loss,
                direction=trade_direction,
                resistance_level=resistance,
                support_level=support,
            )
            risk_reward = calculate_risk_reward_ratio(
                entry_price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                direction=trade_direction,
            )

            sizing = calculate_position_size(
                confidence=confidence,
                risk_reward_ratio=risk_reward,
                portfolio_value=portfolio_value,
                risk_tolerance=risk_tolerance,
            )
            position_size_pct = sizing["position_pct"]
            max_loss_pct = sizing["max_loss_pct"]

            # Skip if Kelly says don't trade (negative expected value)
            if position_size_pct <= 0:
                skipped.append({"ticker": ticker, "reason": "negative expected value (Kelly <= 0)"})
                continue

            # d. Portfolio risk check — gate before approving the idea
            active_ideas = crud.get_active_trade_ideas(self.db)
            risk_check = portfolio_risk_check(
                new_idea={
                    "symbol": ticker,
                    "shares_value": sizing["shares_value"],
                    "direction": trade_direction,
                },
                existing_positions=existing_positions,
                existing_ideas=[
                    {"symbol": i.ticker, "shares_value": i.position_size_pct * portfolio_value}
                    for i in active_ideas
                ],
                portfolio_value=portfolio_value,
            )

            if not risk_check["approved"]:
                logger.info(
                    "TradeIdeaAgent: portfolio risk check rejected %s: %s",
                    ticker,
                    risk_check["rejections"],
                )
                skipped.append({"ticker": ticker, "reason": f"risk check: {risk_check['rejections']}"})
                continue

            # Build key levels from technicals
            key_levels = {
                "sma_50": technicals.get("sma_50"),
                "sma_200": technicals.get("sma_200"),
                "rsi_14": technicals.get("rsi_14"),
                "week_52_high": technicals.get("week_52_high"),
                "week_52_low": technicals.get("week_52_low"),
            }

            # Compute expiry
            expiry_days = _HORIZON_EXPIRY_DAYS.get(horizon, 21)
            expires_at = datetime.now(timezone.utc) + timedelta(days=expiry_days)

            idea = {
                "thesis_id": thesis.get("id", ""),
                "ticker": ticker,
                "direction": trade_direction,
                "strategy": strategy,
                "horizon": horizon,
                "entry_price": round(current_price, 2),
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "current_price": round(current_price, 2),
                "confidence": confidence,
                "risk_reward_ratio": round(risk_reward, 2),
                "position_size_pct": round(position_size_pct, 4),
                "max_loss_pct": round(max_loss_pct, 4),
                "rationale": "",  # filled by Claude in step 3
                "key_levels": json.dumps(key_levels),
                "status": "active",
                "expires_at": expires_at,
            }

            # Attach thesis context for rationale generation (not persisted)
            idea["_thesis_reasoning"] = thesis.get("reasoning", "")
            idea["_thesis_key_risks"] = thesis.get("key_risks", [])
            idea["_technicals"] = technicals

            ideas.append(idea)
            logger.debug(
                "TradeIdeaAgent: built idea for %s — %s @ %.2f, "
                "stop=%.2f, target=%.2f, size=%.1f%%",
                ticker,
                trade_direction,
                current_price,
                stop_loss,
                take_profit,
                position_size_pct * 100,
            )

        return ideas, skipped

    def _fetch_market_data(self, ticker: str, action: str) -> dict | None:
        """
        Fetch market data for a ticker, returning None on failure.
        Wraps execute_market_data with error handling so one bad ticker
        doesn't kill the entire run.
        """
        try:
            result = execute_market_data(action=action, ticker=ticker)
            if "error" in result:
                logger.warning(
                    "TradeIdeaAgent: market_data(%s, %s) returned error: %s",
                    action,
                    ticker,
                    result["error"],
                )
                return None
            return result
        except Exception as exc:
            logger.warning(
                "TradeIdeaAgent: market_data(%s, %s) raised: %s",
                action,
                ticker,
                exc,
            )
            return None

    # ── Rationale generation ─────────────────────────────────────────────────

    def _generate_rationales(self, ideas: list[dict]) -> list[dict]:
        """
        Send a single Claude call to generate rationales for all ideas.
        Falls back to a generic rationale if the API call fails.
        """
        prompt = self._build_rationale_prompt(ideas)

        try:
            response = self._call_api(
                system=RATIONALE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )
            text = self._extract_text(response)
            rationales = self._extract_json(text)

            # rationales should be a dict mapping ticker -> rationale string
            if isinstance(rationales, dict):
                rationale_map = rationales
            elif isinstance(rationales, list):
                # If Claude returns a list, map by index
                rationale_map = {}
                for i, r in enumerate(rationales):
                    if i < len(ideas):
                        key = ideas[i]["ticker"]
                        if isinstance(r, dict):
                            rationale_map[key] = r.get("rationale", str(r))
                        else:
                            rationale_map[key] = str(r)
            else:
                logger.warning(
                    "TradeIdeaAgent: unexpected rationale format: %s",
                    type(rationales),
                )
                rationale_map = {}

            for idea in ideas:
                ticker = idea["ticker"]
                rationale = rationale_map.get(ticker, "")
                if rationale:
                    idea["rationale"] = rationale
                else:
                    idea["rationale"] = self._fallback_rationale(idea)

        except Exception as exc:
            logger.warning(
                "TradeIdeaAgent: rationale generation failed (%s), using fallbacks",
                exc,
            )
            for idea in ideas:
                idea["rationale"] = self._fallback_rationale(idea)

        # Clean up transient keys before persistence
        for idea in ideas:
            idea.pop("_thesis_reasoning", None)
            idea.pop("_thesis_key_risks", None)
            idea.pop("_technicals", None)

        return ideas

    def _build_rationale_prompt(self, ideas: list[dict]) -> str:
        """
        Build a single prompt asking Claude to generate rationales for all ideas.
        Includes thesis reasoning, technicals, and computed levels for each.
        """
        lines = [
            "Generate a rationale for each of the following trade ideas.",
            "For each idea, explain:",
            "- Why this trade (connecting thesis reasoning to the specific price levels)",
            "- What would invalidate it (the stop-loss scenario)",
            "- The key catalyst to watch",
            "",
            "Respond with ONLY a JSON object mapping ticker to rationale string:",
            '```json',
            '{',
            '  "AAPL": "The thesis suggests...",',
            '  "NVDA": "If the thesis plays out..."',
            '}',
            '```',
            "",
            "Here are the trade ideas:",
            "",
        ]

        for idea in ideas:
            technicals = idea.get("_technicals", {})
            key_risks = idea.get("_thesis_key_risks", [])
            if isinstance(key_risks, str):
                try:
                    key_risks = json.loads(key_risks)
                except (json.JSONDecodeError, TypeError):
                    key_risks = [key_risks]

            lines.append(f"### {idea['ticker']} ({idea['direction'].upper()})")
            lines.append(f"Thesis reasoning: {idea.get('_thesis_reasoning', 'N/A')}")
            lines.append(f"Key risks: {', '.join(key_risks) if key_risks else 'N/A'}")
            lines.append(f"Current price: ${idea['current_price']:.2f}")
            lines.append(f"Entry: ${idea['entry_price']:.2f}")
            lines.append(f"Stop-loss: ${idea['stop_loss']:.2f}")
            lines.append(f"Take-profit: ${idea['take_profit']:.2f}")
            lines.append(f"Risk/reward: {idea['risk_reward_ratio']:.2f}")
            lines.append(f"Confidence: {idea['confidence']:.0%}")
            lines.append(f"Horizon: {idea['horizon']}")

            if technicals:
                lines.append(
                    f"Technicals: RSI={technicals.get('rsi_14', 'N/A')}, "
                    f"SMA50={technicals.get('sma_50', 'N/A')}, "
                    f"SMA200={technicals.get('sma_200', 'N/A')}, "
                    f"ATR={technicals.get('atr_14', 'N/A')}"
                )

            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _fallback_rationale(idea: dict) -> str:
        """Generate a basic rationale when Claude is unavailable."""
        direction_word = "upside" if idea["direction"] == "long" else "downside"
        return (
            f"The thesis suggests {direction_word} potential for {idea['ticker']} "
            f"from ${idea['entry_price']:.2f} to ${idea['take_profit']:.2f} "
            f"(risk/reward {idea['risk_reward_ratio']:.1f}:1). "
            f"The idea is invalidated below ${idea['stop_loss']:.2f}."
        )
