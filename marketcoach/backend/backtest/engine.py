"""
Backtest engine — orchestrates the time-machine simulation.

Flow:
  1. Generate decision points (every N days within the window)
  2. At each decision point:
     a. Fetch historical technicals for each ticker (no look-ahead)
     b. Feed them to Claude to generate theses (same prompt as AnalysisAgent)
     c. Convert theses to trade ideas using the same risk math
     d. Open positions in the simulator
  3. Between decision points, simulate daily price checks
  4. At end, close remaining positions and compute metrics

Interview angle:
  This is a pipeline orchestration problem — same pattern as ETL/ELT jobs,
  ML training pipelines, and CI/CD. The key design decision is separating
  "what data to use" (historical_data.py) from "what to do with it"
  (engine.py) from "how to simulate" (simulator.py). This separation lets
  you swap any layer independently — e.g., replace Claude with a rules-based
  system, or swap the simulator for a more sophisticated one with slippage.
"""

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import anthropic

from backend.backtest.historical_data import (
    get_daily_prices,
    get_historical_technicals,
)
from backend.backtest.schemas import BacktestConfig, BacktestResult
from backend.backtest.simulator import PortfolioSimulator
from backend.tools.risk import (
    calculate_position_size,
    calculate_risk_reward_ratio,
    calculate_stop_loss,
    calculate_take_profit,
    classify_horizon,
)

logger = logging.getLogger(__name__)

# Same system prompt as AnalysisAgent — consistency matters for backtesting
BACKTEST_ANALYSIS_PROMPT = """You are a quantitative market analyst generating structured market theses.

You will receive technical indicators for multiple stocks AS OF A SPECIFIC DATE.
Based on these technicals, generate 3-5 market theses for the most actionable opportunities.

IMPORTANT: You are analyzing data from {as_of_date}. Do NOT use any knowledge of events
after this date. Base your analysis ONLY on the technical data provided.

CRITICAL OUTPUT FORMAT — respond with ONLY this JSON (no markdown prose outside the block):
```json
{{
  "theses": [
    {{
      "ticker": "NVDA",
      "direction": "bullish",
      "confidence": 0.72,
      "timeframe": "3-10 days",
      "reasoning": "Technical signals suggest...",
      "key_risks": ["Risk 1", "Risk 2"]
    }}
  ],
  "market_summary": "One-paragraph synthesis of the overall technical landscape"
}}
```

Rules:
- direction must be exactly: bullish, bearish, or neutral
- confidence is 0.0-1.0. Use 0.8+ only when multiple technical signals align strongly.
  Mixed signals should be 0.5-0.65 maximum.
- timeframe examples: "1-3 days", "3-10 days", "2-4 weeks"
- reasoning must cite specific technical levels (RSI, SMA crossovers, MACD, support/resistance)
- key_risks must be concrete (e.g. "RSI divergence suggests momentum may fade")
- Language: use "technicals suggest", "thesis is", "if X holds" — never "will" or "guaranteed"
- Do NOT give direct buy/sell recommendations. Frame as thesis + conditions.
- Prefer tickers where multiple technical signals converge (e.g. RSI oversold + near SMA support)
"""


# Horizon to expiry days — same as TradeIdeaAgent
_HORIZON_EXPIRY_DAYS = {"short": 5, "medium": 21, "long": 60}


def _generate_decision_dates(
    start_date: date, end_date: date, interval_days: int
) -> list[date]:
    """Generate the list of dates when the AI makes decisions."""
    dates = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=interval_days)
    return dates


def _format_technicals_for_claude(all_technicals: dict[str, dict], as_of_date: date) -> str:
    """Format multiple tickers' technicals into a readable table for Claude."""
    lines = [
        f"Technical data as of {as_of_date.isoformat()}:",
        "",
        "Ticker | Price | SMA50 | SMA200 | RSI(14) | MACD Hist | ATR(14) | 52w High | 52w Low",
        "-------|-------|-------|--------|---------|-----------|---------|----------|--------",
    ]

    for ticker, tech in all_technicals.items():
        macd = tech.get("macd", {})
        lines.append(
            f"{ticker} | "
            f"${tech.get('current_price', 'N/A')} | "
            f"${tech.get('sma_50', 'N/A')} | "
            f"${tech.get('sma_200', 'N/A')} | "
            f"{tech.get('rsi_14', 'N/A')} | "
            f"{macd.get('histogram', 'N/A')} | "
            f"{tech.get('atr_14', 'N/A')} | "
            f"${tech.get('week_52_high', 'N/A')} | "
            f"${tech.get('week_52_low', 'N/A')}"
        )

    return "\n".join(lines)


def _parse_theses_response(text: str) -> list[dict]:
    """Parse Claude's JSON response into thesis dicts. Same logic as AnalysisAgent."""
    import re

    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    raw_json = match.group(1) if match else text

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("Failed to parse backtest theses JSON")
        return []

    return parsed.get("theses", [])


def run_backtest(config: BacktestConfig) -> BacktestResult:
    """
    Execute a full time-machine backtest.

    This is the main entry point. It:
      1. Generates decision dates
      2. At each, fetches historical technicals + asks Claude for theses
      3. Converts theses to trades using the same risk math as live
      4. Simulates the portfolio day-by-day
      5. Returns complete results with metrics and equity curve
    """
    backtest_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    logger.info(
        "Starting backtest %s: %s to %s, capital=$%.0f, watchlist=%s",
        backtest_id, config.start_date, config.end_date,
        config.initial_capital, config.watchlist,
    )

    from backend.config import settings
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    simulator = PortfolioSimulator(initial_capital=config.initial_capital)
    decision_dates = _generate_decision_dates(
        config.start_date, config.end_date, config.decision_interval_days,
    )

    total_theses = 0

    # Pre-fetch all daily prices for the full window (one call per ticker)
    # This is much more efficient than fetching per-day
    logger.info("Pre-fetching daily prices for %d tickers...", len(config.watchlist))
    all_daily_prices: dict[str, dict[str, float]] = {}
    for ticker in config.watchlist:
        all_daily_prices[ticker] = get_daily_prices(
            ticker, config.start_date, config.end_date,
        )

    # Get all trading days (union of all tickers' available dates)
    all_trading_days = sorted(set(
        d for prices in all_daily_prices.values() for d in prices.keys()
    ))

    logger.info(
        "Fetched prices: %d trading days, %d decision points",
        len(all_trading_days), len(decision_dates),
    )

    # Main simulation loop
    decision_idx = 0
    for day_str in all_trading_days:
        day = date.fromisoformat(day_str)

        # Check if this is a decision point
        if decision_idx < len(decision_dates) and day >= decision_dates[decision_idx]:
            logger.info("Decision point %d/%d: %s", decision_idx + 1, len(decision_dates), day)

            theses = _run_decision_point(
                client=client,
                watchlist=config.watchlist,
                decision_date=day,
                simulator=simulator,
                risk_tolerance=config.risk_tolerance,
                all_daily_prices=all_daily_prices,
            )
            total_theses += len(theses)
            decision_idx += 1

        # Process daily prices for stop/target checks
        day_prices = {
            ticker: prices.get(day_str, 0.0)
            for ticker, prices in all_daily_prices.items()
            if day_str in prices
        }
        simulator.process_day(day_str, day_prices)

    # Close remaining positions at end
    if all_trading_days:
        final_prices = {
            ticker: prices.get(all_trading_days[-1], 0.0)
            for ticker, prices in all_daily_prices.items()
            if all_trading_days[-1] in prices
        }
        simulator.close_all_positions(all_trading_days[-1], final_prices)

    completed_at = datetime.now(timezone.utc)

    return BacktestResult(
        id=backtest_id,
        config=config,
        metrics=simulator.get_metrics(),
        equity_curve=simulator.equity_curve,
        trades=simulator.closed_trades,
        decision_points=len(decision_dates),
        total_theses_generated=total_theses,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )


def _run_decision_point(
    client: anthropic.Anthropic,
    watchlist: list[str],
    decision_date: date,
    simulator: PortfolioSimulator,
    risk_tolerance: str,
    all_daily_prices: dict[str, dict[str, float]],
) -> list[dict]:
    """
    Run one decision point: fetch technicals, generate theses, open positions.
    Returns the theses generated.
    """
    from backend.agents.base import MODEL

    # 1. Fetch historical technicals for each ticker
    all_technicals: dict[str, dict] = {}
    for ticker in watchlist:
        tech = get_historical_technicals(ticker, decision_date)
        if tech:
            all_technicals[ticker] = tech

    if not all_technicals:
        logger.warning("No technical data available for decision date %s", decision_date)
        return []

    # 2. Ask Claude to generate theses from the technicals
    formatted = _format_technicals_for_claude(all_technicals, decision_date)
    system_prompt = BACKTEST_ANALYSIS_PROMPT.format(as_of_date=decision_date.isoformat())

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Here are the technical indicators for {len(all_technicals)} tickers "
                    f"as of {decision_date.isoformat()}:\n\n{formatted}\n\n"
                    "Generate market theses based on these technicals."
                ),
            }],
        )
    except Exception as exc:
        logger.warning("Claude call failed for decision date %s: %s", decision_date, exc)
        return []

    # Extract text from response
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    theses = _parse_theses_response(text)
    if not theses:
        logger.warning("No theses generated for %s", decision_date)
        return []

    logger.info("Generated %d theses for %s", len(theses), decision_date)

    # 3. Convert theses to trade ideas and open positions
    current_equity = simulator.equity_curve[-1].equity if simulator.equity_curve else simulator.initial_capital

    for thesis in theses:
        ticker = thesis.get("ticker", "").upper()
        direction = thesis.get("direction", "neutral").lower()
        confidence = thesis.get("confidence", 0.5)
        timeframe = thesis.get("timeframe", "3-10 days")
        reasoning = thesis.get("reasoning", "")

        if direction == "neutral" or not ticker:
            continue

        # Get technicals for this ticker
        tech = all_technicals.get(ticker)
        if not tech:
            continue

        current_price = tech["current_price"]
        if not current_price or current_price <= 0:
            continue

        trade_direction = "long" if direction == "bullish" else "short"
        horizon = classify_horizon(timeframe)

        # Same risk calculations as TradeIdeaAgent
        atr = tech.get("atr_14")
        support = tech.get("week_52_low")
        resistance = tech.get("week_52_high")

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
            portfolio_value=current_equity,
            risk_tolerance=risk_tolerance,
        )

        if sizing["position_pct"] <= 0:
            continue

        # Calculate expiry date
        expiry_days = _HORIZON_EXPIRY_DAYS.get(horizon, 21)
        expires_on = (decision_date + timedelta(days=expiry_days)).isoformat()

        simulator.open_position(
            ticker=ticker,
            direction=trade_direction,
            entry_date=decision_date.isoformat(),
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size_pct=sizing["position_pct"],
            confidence=confidence,
            thesis_direction=direction,
            thesis_reasoning=reasoning[:300],
            expires_on=expires_on,
        )

    return theses
