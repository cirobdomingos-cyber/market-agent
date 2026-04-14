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
        "margins), price history, and side-by-side ticker comparison."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["quote", "technicals", "fundamentals", "price_history", "compare"],
                "description": (
                    "The type of data to retrieve. "
                    "'quote' = current price, change, volume, market cap. "
                    "'technicals' = SMA, RSI, MACD, ATR, 52-week range. "
                    "'fundamentals' = P/E, EPS, revenue, margins, dividend, sector. "
                    "'price_history' = daily close prices for the last N days. "
                    "'compare' = side-by-side metrics for 2–3 tickers."
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
        },
        "required": ["action"],
    },
}


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


# Dispatch table — keeps execute_market_data() clean.
_ACTION_HANDLERS = {
    "quote": lambda params: _action_quote(params["ticker"]),
    "technicals": lambda params: _action_technicals(params["ticker"]),
    "fundamentals": lambda params: _action_fundamentals(params["ticker"]),
    "price_history": lambda params: _action_price_history(params["ticker"], params.get("days", 30)),
    "compare": lambda params: _action_compare(params["tickers"]),
}


def execute_market_data(
    action: str,
    ticker: str | None = None,
    tickers: list[str] | None = None,
    days: int = 30,
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
    else:
        if not ticker:
            return {"error": f"The '{action}' action requires a 'ticker' parameter."}

    params = {"ticker": ticker, "tickers": tickers or [], "days": days}

    try:
        result = _ACTION_HANDLERS[action](params)
        logger.debug("market_data(%s, %s) → success", action, ticker or tickers)
        return result
    except Exception as exc:
        logger.warning("market_data(%s, %s) failed: %s", action, ticker or tickers, exc)
        return {"error": str(exc), "action": action, "ticker": ticker, "tickers": tickers}
