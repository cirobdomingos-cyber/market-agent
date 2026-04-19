"""
Market data tool powered by yfinance for the Claude agentic loop.

Provides real-time quotes, technical indicators, fundamentals, price history,
and multi-ticker comparison. Claude agents call this via tool_use the same way
they call web_search — the agentic loop dispatches to execute_market_data().
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Tool definition passed to Claude — matches the standard tool_use schema.
MARKET_DATA_TOOL: dict = {
    "name": "market_data",
    "description": (
        "Fetch real-time market data, fundamentals, and technical indicators for "
        "stocks and ETFs using ticker symbols. Supports current quotes, technical "
        "analysis (SMA, RSI, MACD, ATR), fundamental metrics (P/E, EPS, revenue, "
        "margins), price history, side-by-side ticker comparison, and ATR-based "
        "bracket level suggestions (stop + target anchored to volatility)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "quote",
                    "technicals",
                    "fundamentals",
                    "price_history",
                    "compare",
                    "suggest_bracket",
                ],
                "description": (
                    "The type of data to retrieve. "
                    "'quote' = current price, change, volume, market cap. "
                    "'technicals' = SMA, RSI, MACD, ATR, 52-week range. "
                    "'fundamentals' = P/E, EPS, revenue, margins, dividend, sector. "
                    "'price_history' = daily close prices for the last N days. "
                    "'compare' = side-by-side metrics for 2–3 tickers. "
                    "'suggest_bracket' = ATR-based stop + target suggestion "
                    "for a proposed entry. Use this as the starting anchor "
                    "for any bracket order; adjust from there to respect "
                    "technical levels (support below, resistance above)."
                ),
            },
            "ticker": {
                "type": "string",
                "description": "Stock or ETF ticker symbol (e.g. 'AAPL', 'SPY'). Required for all actions except 'compare'.",
            },
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of 2–3 ticker symbols for the 'compare' action.",
            },
            "days": {
                "type": "integer",
                "description": "Number of days of price history to return. Default: 30. Only used with 'price_history'.",
                "default": 30,
            },
            "entry_price": {
                "type": "number",
                "description": (
                    "Proposed entry price for the 'suggest_bracket' action. "
                    "Usually the current ask for a market buy or the chosen "
                    "limit price for a limit buy."
                ),
            },
            "horizon": {
                "type": "string",
                "enum": ["day", "swing", "position"],
                "description": (
                    "Trade horizon for the 'suggest_bracket' action. "
                    "'day' = intraday (1× ATR stop). "
                    "'swing' = 2-20 days (2× ATR, DEFAULT). "
                    "'position' = 1-6 months (3× ATR, wider to absorb noise)."
                ),
                "default": "swing",
            },
        },
        "required": ["action"],
    },
}


# ATR multipliers per horizon. Tunable in one place if someone later wants
# tighter day-trader defaults or wider position-trade defaults.
_ATR_MULTIPLIERS = {
    "day": 1.0,
    "swing": 2.0,
    "position": 3.0,
}
# Reward:risk ratios for the suggested targets. 2:1 is the minimum the
# trading advisor is allowed to recommend; 3:1 is the "great setup" level.
_RR_T1 = 2.0
_RR_T2 = 3.0


def _safe_get(info: dict, key: str, default=None):
    """Extract a value from yfinance info dict, returning default if missing or None."""
    val = info.get(key)
    return val if val is not None else default


def _get_ticker(symbol: str):
    """Create a yfinance Ticker object from a symbol string."""
    import yfinance as yf  # lazy import — pandas is heavy at startup
    return yf.Ticker(symbol.strip().upper())


def _action_quote(ticker: str) -> dict:
    """Current price, change, volume, and market cap."""
    t = _get_ticker(ticker)
    info = t.info

    if not info or info.get("regularMarketPrice") is None:
        return {"error": f"No data found for ticker '{ticker}'. Verify the symbol is correct."}

    price = _safe_get(info, "regularMarketPrice", 0)
    prev_close = _safe_get(info, "regularMarketPreviousClose", 0)
    change = round(price - prev_close, 4) if price and prev_close else None
    change_pct = round((change / prev_close) * 100, 2) if change and prev_close else None

    return {
        "ticker": ticker.upper(),
        "name": _safe_get(info, "shortName", ""),
        "price": price,
        "currency": _safe_get(info, "currency", "USD"),
        "change": change,
        "change_percent": change_pct,
        "volume": _safe_get(info, "regularMarketVolume"),
        "market_cap": _safe_get(info, "marketCap"),
        "exchange": _safe_get(info, "exchange", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _compute_rsi(closes, period: int = 14) -> float | None:
    """Calculate Relative Strength Index from a pandas Series of close prices."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gains = delta.where(delta > 0, 0.0)
    losses = (-delta.where(delta < 0, 0.0))
    avg_gain = gains.rolling(window=period).mean()
    avg_loss = losses.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if val == val else None  # NaN check


def _compute_atr(hist, period: int = 14) -> float | None:
    """Calculate Average True Range from OHLC DataFrame."""
    if len(hist) < period + 1:
        return None
    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    prev_close = close.shift(1)
    tr = (high - low).combine_first(
        (high - prev_close).abs()
    ).combine_first(
        (low - prev_close).abs()
    )
    # True range is the max of the three components
    import pandas as pd
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    val = atr.iloc[-1]
    return round(float(val), 2) if val == val else None


def _action_technicals(ticker: str) -> dict:
    """50/200 day SMA, RSI(14), MACD, 52-week high/low, ATR(14)."""
    t = _get_ticker(ticker)
    info = t.info

    if not info or info.get("regularMarketPrice") is None:
        return {"error": f"No data found for ticker '{ticker}'. Verify the symbol is correct."}

    # Fetch 1 year of daily history for indicator calculations
    hist = t.history(period="1y")
    if hist.empty:
        return {"error": f"No price history available for '{ticker}'."}

    closes = hist["Close"]

    # SMAs
    sma_50 = round(float(closes.rolling(50).mean().iloc[-1]), 2) if len(closes) >= 50 else None
    sma_200 = round(float(closes.rolling(200).mean().iloc[-1]), 2) if len(closes) >= 200 else None

    # MACD (12/26/9)
    ema_12 = closes.ewm(span=12, adjust=False).mean()
    ema_26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_histogram = macd_line - signal_line

    return {
        "ticker": ticker.upper(),
        "sma_50": sma_50,
        "sma_200": sma_200,
        "rsi_14": _compute_rsi(closes, 14),
        "macd": {
            "macd_line": round(float(macd_line.iloc[-1]), 4),
            "signal_line": round(float(signal_line.iloc[-1]), 4),
            "histogram": round(float(macd_histogram.iloc[-1]), 4),
        },
        "atr_14": _compute_atr(hist, 14),
        "week_52_high": _safe_get(info, "fiftyTwoWeekHigh"),
        "week_52_low": _safe_get(info, "fiftyTwoWeekLow"),
        "current_price": _safe_get(info, "regularMarketPrice"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _action_fundamentals(ticker: str) -> dict:
    """P/E ratio, EPS, revenue, profit margins, dividend yield, sector."""
    t = _get_ticker(ticker)
    info = t.info

    if not info or not info.get("shortName"):
        return {"error": f"No data found for ticker '{ticker}'. Verify the symbol is correct."}

    return {
        "ticker": ticker.upper(),
        "name": _safe_get(info, "shortName", ""),
        "sector": _safe_get(info, "sector"),
        "industry": _safe_get(info, "industry"),
        "pe_trailing": _safe_get(info, "trailingPE"),
        "pe_forward": _safe_get(info, "forwardPE"),
        "eps_trailing": _safe_get(info, "trailingEps"),
        "eps_forward": _safe_get(info, "forwardEps"),
        "revenue": _safe_get(info, "totalRevenue"),
        "gross_margin": _safe_get(info, "grossMargins"),
        "operating_margin": _safe_get(info, "operatingMargins"),
        "profit_margin": _safe_get(info, "profitMargins"),
        "dividend_yield": _safe_get(info, "dividendYield"),
        "beta": _safe_get(info, "beta"),
        "market_cap": _safe_get(info, "marketCap"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _action_price_history(ticker: str, days: int = 30) -> dict:
    """Last N days of daily close prices."""
    days = min(max(1, days), 365)  # clamp to 1–365

    t = _get_ticker(ticker)
    hist = t.history(period=f"{days}d")

    if hist.empty:
        return {"error": f"No price history available for '{ticker}'. Verify the symbol is correct."}

    prices = []
    for date, row in hist.iterrows():
        prices.append({
            "date": date.strftime("%Y-%m-%d"),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]),
        })

    return {
        "ticker": ticker.upper(),
        "days_requested": days,
        "days_returned": len(prices),
        "prices": prices,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _action_compare(tickers: list[str]) -> dict:
    """Compare 2–3 tickers on key metrics side-by-side."""
    if not tickers or len(tickers) < 2:
        return {"error": "Provide at least 2 tickers for comparison."}
    if len(tickers) > 3:
        tickers = tickers[:3]  # cap at 3

    comparison = {}
    for symbol in tickers:
        symbol = symbol.strip().upper()
        try:
            t = _get_ticker(symbol)
            info = t.info
            if not info or info.get("regularMarketPrice") is None:
                comparison[symbol] = {"error": f"No data found for '{symbol}'."}
                continue

            price = _safe_get(info, "regularMarketPrice", 0)
            prev_close = _safe_get(info, "regularMarketPreviousClose", 0)
            change_pct = round(((price - prev_close) / prev_close) * 100, 2) if price and prev_close else None

            comparison[symbol] = {
                "name": _safe_get(info, "shortName", ""),
                "price": price,
                "change_percent": change_pct,
                "market_cap": _safe_get(info, "marketCap"),
                "pe_trailing": _safe_get(info, "trailingPE"),
                "eps_trailing": _safe_get(info, "trailingEps"),
                "dividend_yield": _safe_get(info, "dividendYield"),
                "beta": _safe_get(info, "beta"),
                "week_52_high": _safe_get(info, "fiftyTwoWeekHigh"),
                "week_52_low": _safe_get(info, "fiftyTwoWeekLow"),
                "sector": _safe_get(info, "sector"),
            }
        except Exception as exc:
            logger.warning("compare: failed to fetch %s: %s", symbol, exc)
            comparison[symbol] = {"error": str(exc)}

    return {
        "tickers": list(comparison.keys()),
        "comparison": comparison,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _action_suggest_bracket(
    ticker: str,
    entry_price: float,
    horizon: str = "swing",
) -> dict:
    """
    Volatility-anchored stop + target suggestion for a proposed BUY entry.

    Stop = entry − N × ATR(14), where N is the horizon's multiplier.
    Target 1 = entry + (2 × risk) — classic 2:1 reward:risk.
    Target 2 = entry + (3 × risk) — optional scale-out level.

    Why ATR instead of percent:
      A 2% stop on SPY (~$11 move) would rarely trigger; on TSLA (~$8 move)
      it gets hit by normal intraday noise. ATR normalises: "2× ATR"
      means the same thing on both — about how much the stock typically
      moves in two days, so your stop respects the asset's rhythm.

    Returns a dict the advisor can weave into its trade decision matrix.
    The advisor is expected to ADJUST these numbers based on technical
    levels (stop just below a real support, target at a real resistance
    when those are tighter/wider than the ATR suggestion). This is the
    starting anchor, not the final answer.
    """
    if entry_price is None or entry_price <= 0:
        return {"error": "entry_price must be a positive number."}

    horizon_key = (horizon or "swing").strip().lower()
    if horizon_key not in _ATR_MULTIPLIERS:
        return {
            "error": (
                f"Unknown horizon '{horizon}'. "
                f"Valid: {list(_ATR_MULTIPLIERS.keys())}"
            ),
        }
    atr_mult = _ATR_MULTIPLIERS[horizon_key]

    t = _get_ticker(ticker)
    hist = t.history(period="1y")
    if hist.empty:
        return {
            "error": f"No price history for '{ticker}' — cannot compute ATR.",
            "suggested_fallback": "Use a 2% default stop if no better anchor is available.",
        }

    atr = _compute_atr(hist, 14)
    if atr is None or atr <= 0:
        return {
            "error": f"ATR(14) unavailable for '{ticker}' (thin history or data error).",
            "suggested_fallback": "Use a 2% default stop if no better anchor is available.",
        }

    risk_per_share = atr * atr_mult
    stop_loss = round(entry_price - risk_per_share, 2)
    target_1 = round(entry_price + risk_per_share * _RR_T1, 2)
    target_2 = round(entry_price + risk_per_share * _RR_T2, 2)

    # Safety: if the ATR stop would be at or below zero (penny stocks,
    # data error), refuse rather than emit nonsense.
    if stop_loss <= 0:
        return {
            "error": (
                f"ATR-based stop ({stop_loss}) is non-positive for {ticker} "
                f"at entry ${entry_price}. ATR ({atr}) × multiplier "
                f"({atr_mult}) is too wide relative to the entry price. "
                "Ticker may be a penny stock or the data is malformed."
            ),
        }

    reasoning = (
        f"Stop at {atr_mult:.1f}× ATR-14 (${risk_per_share:.2f}) below "
        f"entry, accommodating this ticker's typical {horizon_key} "
        f"volatility. Target 1 at {_RR_T1:.0f}:1 reward:risk "
        f"(${risk_per_share * _RR_T1:.2f} above entry). Target 2 at "
        f"{_RR_T2:.0f}:1 for an optional scale-out runner. "
        "ADJUST from here to respect technical levels — a stop just "
        "below real support, or a target at real resistance, should "
        "override these anchors when those levels are tighter or wider."
    )

    return {
        "ticker": ticker.upper(),
        "entry": round(float(entry_price), 2),
        "horizon": horizon_key,
        "atr_14": round(float(atr), 2),
        "atr_multiplier": atr_mult,
        "risk_per_share": round(float(risk_per_share), 2),
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "rr_target_1": _RR_T1,
        "rr_target_2": _RR_T2,
        "reasoning": reasoning,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Dispatch table — keeps execute_market_data() clean.
_ACTION_HANDLERS = {
    "quote": lambda params: _action_quote(params["ticker"]),
    "technicals": lambda params: _action_technicals(params["ticker"]),
    "fundamentals": lambda params: _action_fundamentals(params["ticker"]),
    "price_history": lambda params: _action_price_history(params["ticker"], params.get("days", 30)),
    "compare": lambda params: _action_compare(params["tickers"]),
    "suggest_bracket": lambda params: _action_suggest_bracket(
        params["ticker"],
        params.get("entry_price"),
        params.get("horizon", "swing"),
    ),
}


def execute_market_data(
    action: str,
    ticker: str | None = None,
    tickers: list[str] | None = None,
    days: int = 30,
    entry_price: float | None = None,
    horizon: str = "swing",
) -> dict:
    """
    Execute a market data action and return a result dict.

    This is the single entry point called by the agentic loop when Claude
    invokes the market_data tool. Mirrors the pattern of execute_web_search().

    Returns:
        dict with action-specific data, or {"error": ...} on failure.
    """
    action = action.strip().lower()

    if action not in _ACTION_HANDLERS:
        return {"error": f"Unknown action '{action}'. Valid: {list(_ACTION_HANDLERS.keys())}"}

    # Validate required params per action
    if action == "compare":
        if not tickers:
            return {"error": "The 'compare' action requires a 'tickers' array with 2–3 symbols."}
    elif action == "suggest_bracket":
        if not ticker:
            return {"error": "The 'suggest_bracket' action requires a 'ticker' parameter."}
        if entry_price is None:
            return {"error": "The 'suggest_bracket' action requires an 'entry_price' parameter."}
    else:
        if not ticker:
            return {"error": f"The '{action}' action requires a 'ticker' parameter."}

    params = {
        "ticker": ticker,
        "tickers": tickers or [],
        "days": days,
        "entry_price": entry_price,
        "horizon": horizon,
    }

    try:
        result = _ACTION_HANDLERS[action](params)
        logger.debug("market_data(%s, %s) → success", action, ticker or tickers)
        return result
    except Exception as exc:
        logger.warning("market_data(%s, %s) failed: %s", action, ticker or tickers, exc)
        return {"error": str(exc), "action": action, "ticker": ticker, "tickers": tickers}
