"""
AnalysisAgent — Phase 2

Responsibilities:
  1. Read recent signals from the database (last N hours)
  2. Ask Claude to synthesise those signals into 3–5 market theses
  3. Persist Thesis rows to the database
  4. Auto-resolve expired theses by comparing to actual price movement

Thesis schema mirrors the spec exactly — it's what gets tracked for accuracy
over time, which is the core feedback loop that builds trust in the system.

Interview angle:
  Structured output from LLMs is a recurring interview topic. The pattern here
  (ask for JSON with a precise schema, validate with Pydantic, store in DB) is
  the standard production approach. The alternative — asking Claude to output
  Python code — is fragile and harder to validate.

  The auto-resolution system is a closed feedback loop: generate thesis ->
  wait for expiry -> compare prediction vs reality -> store accuracy. This is
  the same pattern used in ML model monitoring (predict -> observe -> score).
  Interviewers love seeing self-correcting systems because it shows you think
  beyond "deploy and forget".
"""

import json
import logging
from datetime import datetime, timezone

import anthropic
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.base import AgentResult, BaseAgent
from backend.db import crud
from backend.db.models import Signal

logger = logging.getLogger(__name__)

# ── Output schema ─────────────────────────────────────────────────────────────

class RawThesis(BaseModel):
    ticker: str
    direction: str     # bullish | bearish | neutral
    confidence: float  # 0.0 – 1.0
    timeframe: str     # e.g. "3–10 days"
    reasoning: str
    key_risks: list[str]


class AnalysisAgentOutput(BaseModel):
    theses: list[RawThesis]
    market_summary: str
    analysed_at: datetime


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a quantitative market analyst generating structured market theses.

You will receive a list of market signals extracted from recent news.
Based on these signals, generate 3–5 market theses for the most actionable opportunities.

CRITICAL OUTPUT FORMAT — respond with ONLY this JSON (no markdown prose outside the block):
```json
{
  "theses": [
    {
      "ticker": "NVDA",
      "direction": "bullish",
      "confidence": 0.72,
      "timeframe": "3-10 days",
      "reasoning": "Multiple signals suggest...",
      "key_risks": ["Risk 1", "Risk 2"]
    }
  ],
  "market_summary": "One-paragraph synthesis of the overall market environment"
}
```

Rules:
- direction must be exactly: bullish, bearish, or neutral
- confidence is 0.0–1.0. Use 0.8+ only when multiple independent signals agree.
  Single-source signals should be 0.5–0.65 maximum.
- timeframe examples: "1-3 days", "3-10 days", "2-4 weeks", "1-3 months"
- reasoning must cite specific signals, not generic platitudes
- key_risks must be concrete and specific (e.g. "Fed statement on Thursday could reverse momentum")
- Language: use "signals suggest", "thesis is", "if X holds" — never "will" or "guaranteed"
- Do NOT give direct buy/sell recommendations. Frame as thesis + conditions.
- Generate theses only for tickers with at least 2 corroborating signals OR 1 very high-confidence signal (0.85+)
"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class AnalysisAgent(BaseAgent):
    """
    Generates market theses from recent signals.
    Does NOT use tools — it reasons purely from the signals passed in context.
    """

    def __init__(
        self,
        db: Session,
        client: anthropic.Anthropic,
        signal_lookback_hours: int = 4,
    ):
        super().__init__(db, client)
        self.signal_lookback_hours = signal_lookback_hours

    def run(self, context: dict) -> AgentResult:
        signals = crud.get_recent_signals(self.db, hours=self.signal_lookback_hours)

        if not signals:
            logger.info("AnalysisAgent: no recent signals found, skipping")
            return AgentResult(
                success=True,
                data={"theses": [], "market_summary": "No recent signals available."},
            )

        try:
            output = self._generate_theses(signals)
            stored = self._store_theses(output.theses)

            logger.info(
                "AnalysisAgent: generated %d theses from %d signals, stored %d",
                len(output.theses),
                len(signals),
                stored,
            )
            return AgentResult(success=True, data=output.model_dump(mode="json"))

        except Exception as exc:
            logger.exception("AnalysisAgent failed")
            return AgentResult(success=False, data={}, error=str(exc))

    # ── Core logic ────────────────────────────────────────────────────────────

    def _generate_theses(self, signals: list[Signal]) -> AnalysisAgentOutput:
        signals_text = self._format_signals(signals)

        messages = [{
            "role": "user",
            "content": (
                f"Here are the recent market signals ({len(signals)} total, "
                f"last {self.signal_lookback_hours} hours):\n\n"
                f"{signals_text}\n\n"
                "Generate market theses based on these signals."
            ),
        }]

        # Use the base class agentic loop — no tools needed for pure reasoning.
        # Even with tools=[], the loop handles the send/receive cycle correctly
        # and returns on end_turn.
        response = self._agentic_loop(
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=[],
        )

        text = self._extract_text(response)
        raw = self._extract_json(text)

        theses = [RawThesis(**t) for t in raw["theses"]]
        return AnalysisAgentOutput(
            theses=theses,
            market_summary=raw.get("market_summary", ""),
            analysed_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _format_signals(signals: list[Signal]) -> str:
        """Format signals as a readable table for Claude."""
        lines = ["ticker | sentiment | confidence | source | headline"]
        lines.append("-------|-----------|------------|--------|----------")
        for s in signals:
            lines.append(
                f"{s.ticker} | {s.sentiment} | {s.confidence:.2f} | "
                f"{s.source} | {s.headline[:80]}"
            )
        return "\n".join(lines)

    # ── DB persistence ────────────────────────────────────────────────────────

    def _store_theses(self, theses: list[RawThesis]) -> int:
        """Persist theses to DB in a single transaction. Returns count inserted."""
        return crud.create_theses_batch(
            self.db,
            [
                dict(
                    ticker=t.ticker.upper(),
                    direction=t.direction,
                    confidence=t.confidence,
                    timeframe=t.timeframe,
                    reasoning=t.reasoning,
                    key_risks=json.dumps(t.key_risks),
                )
                for t in theses
            ],
        )

    # ── Thesis resolution ────────────────────────────────────────────────────

    def resolve_expired_theses(self) -> int:
        """
        Check theses past their timeframe and resolve them vs actual price.

        For each expired thesis:
          1. Fetch price history via yfinance
          2. Find the close price nearest to thesis creation date
          3. Compare with the most recent close
          4. Score the thesis direction as correct/incorrect

        Returns count of theses resolved.
        """
        open_theses = crud.get_open_theses(self.db)
        resolved = 0

        for thesis in open_theses:
            if not self._is_expired(thesis):
                continue

            try:
                outcome, accuracy = self._evaluate_thesis(thesis)
                crud.resolve_thesis(self.db, thesis.id, outcome=outcome, accuracy=accuracy)
                resolved += 1

                logger.info(
                    "Resolved thesis %s: ticker=%s direction=%s "
                    "price_change=%.2f%% outcome=%s accuracy=%.2f",
                    thesis.id,
                    thesis.ticker,
                    thesis.direction,
                    accuracy * 100,
                    outcome,
                    accuracy,
                )

            except Exception as exc:
                # Don't let one bad ticker block the rest.
                # Log and move on — the thesis stays open for next cycle.
                logger.warning(
                    "Failed to resolve thesis %s for %s: %s",
                    thesis.id,
                    thesis.ticker,
                    exc,
                )

        return resolved

    def _evaluate_thesis(self, thesis) -> tuple[str, float]:
        """
        Evaluate a single thesis against actual price movement.

        Returns:
            (outcome, accuracy) where outcome is "correct" or "incorrect"
            and accuracy is the percentage price change in the predicted
            direction (positive = moved as predicted, negative = moved against).

        Raises:
            ValueError: if price data is unavailable for the ticker.
        """
        import yfinance as yf  # lazy import — pandas is heavy
        ticker_obj = yf.Ticker(thesis.ticker)
        hist = ticker_obj.history(period="1mo")

        if hist.empty:
            raise ValueError(f"No price data returned for {thesis.ticker}")

        # Find the close price nearest to thesis creation date.
        # Thesis created_at may fall on a weekend/holiday — use the nearest
        # trading day that exists in the history.
        created = thesis.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        # hist.index is DatetimeIndex (tz-aware from yfinance).
        # Find the nearest date on or after creation, falling back to the
        # closest prior date if creation is newer than all history rows.
        creation_price = self._find_nearest_close(hist, created)
        current_price = hist["Close"].iloc[-1]

        if creation_price == 0:
            raise ValueError(f"Creation price is zero for {thesis.ticker}")

        # Percentage change from creation to now
        pct_change = (current_price - creation_price) / creation_price

        direction = thesis.direction.lower()
        outcome, accuracy = self._score_direction(direction, pct_change)

        return outcome, accuracy

    @staticmethod
    def _find_nearest_close(hist, target_dt: datetime) -> float:
        """
        Find the closing price nearest to target_dt in a yfinance history
        DataFrame. Handles weekends and holidays by picking the closest
        available trading day.

        The hist.index from yfinance is a tz-aware DatetimeIndex.
        """
        import pandas as pd

        target = pd.Timestamp(target_dt).tz_convert(hist.index.tz)

        # Get absolute time differences to find nearest trading day
        diffs = (hist.index - target).to_series().abs()
        nearest_idx = diffs.idxmin()

        return float(hist.loc[nearest_idx, "Close"])

    @staticmethod
    def _score_direction(direction: str, pct_change: float) -> tuple[str, float]:
        """
        Determine if a thesis was correct based on direction and actual
        price change.

        Returns:
            (outcome, accuracy) where accuracy is the pct_change expressed
            relative to the predicted direction. Positive means the price
            moved in the predicted direction.

        Scoring logic:
          - bullish + price went up   -> correct,  accuracy = +pct_change
          - bullish + price went down -> incorrect, accuracy = +pct_change (negative)
          - bearish + price went down -> correct,  accuracy = -pct_change (positive)
          - bearish + price went up   -> incorrect, accuracy = -pct_change (negative)
          - neutral + within +/-2%    -> correct,  accuracy = 1.0 - abs(pct_change)/0.02
          - neutral + outside +/-2%   -> incorrect, accuracy = -abs(pct_change)
        """
        if direction == "bullish":
            # Accuracy = how much it moved in the bullish direction
            accuracy = pct_change
            outcome = "correct" if pct_change > 0 else "incorrect"

        elif direction == "bearish":
            # Flip sign: negative price change = good for bearish thesis
            accuracy = -pct_change
            outcome = "correct" if pct_change < 0 else "incorrect"

        elif direction == "neutral":
            if abs(pct_change) <= 0.02:
                # Price stayed within +/-2% band — neutral was correct.
                # Accuracy scales from 1.0 (no move) to 0.0 (right at boundary).
                accuracy = 1.0 - (abs(pct_change) / 0.02)
                outcome = "correct"
            else:
                # Price moved outside the neutral band — thesis was wrong.
                accuracy = -abs(pct_change)
                outcome = "incorrect"

        else:
            # Unknown direction — shouldn't happen but handle gracefully
            logger.warning("Unknown direction '%s', marking as incorrect", direction)
            accuracy = 0.0
            outcome = "incorrect"

        return outcome, round(accuracy, 4)

    @staticmethod
    def _is_expired(thesis) -> bool:
        """Parse timeframe string and check if it has elapsed."""
        import re
        from datetime import timedelta

        if thesis.resolved_at is not None:
            return False

        # Parse "3-10 days", "2-4 weeks", "1-3 months"
        match = re.search(r"(\d+)[^\d]+(\d+)\s*(day|week|month)", thesis.timeframe, re.I)
        if not match:
            return False

        max_val = int(match.group(2))
        unit = match.group(3).lower()

        if unit.startswith("day"):
            delta = timedelta(days=max_val)
        elif unit.startswith("week"):
            delta = timedelta(weeks=max_val)
        else:
            delta = timedelta(days=max_val * 30)

        created = thesis.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        return datetime.now(timezone.utc) > created + delta
