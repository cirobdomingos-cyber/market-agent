"""
Historical market data — fetch technicals as-of a specific past date.

This is the key to avoiding look-ahead bias: we only compute indicators
from data that existed on the decision date. yfinance supports date-range
queries, so we fetch 1 year of history ending on the decision date and
compute SMA/RSI/MACD/ATR from that window.

Interview angle:
  Look-ahead bias is the #1 mistake in backtesting. It means accidentally
  using future information when making past decisions. The fix is simple
  but easy to forget: always slice your data with end_date <= decision_date.
  This is the same principle behind sklearn's TimeSeriesSplit.
"""

import logging
from datetime import date, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


def _compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
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
    return round(float(val), 2) if val == val else None


def _compute_atr(hist: pd.DataFrame, period: int = 14) -> float | None:
    if len(hist) < period + 1:
        return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period).mean()
    val = atr.iloc[-1]
    return round(float(val), 2) if val == val else None


def get_historical_technicals(ticker: str, as_of_date: date) -> dict | None:
    """
    Compute technical indicators for a ticker using only data available
    on as_of_date. Returns None if insufficient data.

    Fetches ~1 year of history ending on as_of_date to have enough data
    for 200-day SMA calculation.
    """
    import yfinance as yf

    start = as_of_date - timedelta(days=400)  # ~13 months for 200-day SMA
    end = as_of_date + timedelta(days=1)  # yfinance end is exclusive

    try:
        t = yf.Ticker(ticker.upper())
        hist = t.history(start=start.isoformat(), end=end.isoformat())
    except Exception as exc:
        logger.warning("Failed to fetch history for %s as of %s: %s", ticker, as_of_date, exc)
        return None

    if hist.empty or len(hist) < 30:
        logger.warning("Insufficient history for %s as of %s (%d rows)", ticker, as_of_date, len(hist))
        return None

    closes = hist["Close"]
    current_price = float(closes.iloc[-1])

    # SMAs
    sma_50 = round(float(closes.rolling(50).mean().iloc[-1]), 2) if len(closes) >= 50 else None
    sma_200 = round(float(closes.rolling(200).mean().iloc[-1]), 2) if len(closes) >= 200 else None

    # MACD
    ema_12 = closes.ewm(span=12, adjust=False).mean()
    ema_26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_histogram = macd_line - signal_line

    # 52-week high/low (from available data, not the real one — avoids look-ahead)
    year_data = closes.iloc[-252:] if len(closes) >= 252 else closes
    week_52_high = round(float(year_data.max()), 2)
    week_52_low = round(float(year_data.min()), 2)

    return {
        "ticker": ticker.upper(),
        "current_price": round(current_price, 2),
        "sma_50": sma_50,
        "sma_200": sma_200,
        "rsi_14": _compute_rsi(closes, 14),
        "macd": {
            "macd_line": round(float(macd_line.iloc[-1]), 4),
            "signal_line": round(float(signal_line.iloc[-1]), 4),
            "histogram": round(float(macd_histogram.iloc[-1]), 4),
        },
        "atr_14": _compute_atr(hist, 14),
        "week_52_high": week_52_high,
        "week_52_low": week_52_low,
        "as_of_date": as_of_date.isoformat(),
    }


def get_price_on_date(ticker: str, target_date: date) -> float | None:
    """
    Get the closing price for a ticker on a specific date.
    If the market was closed, returns the nearest prior trading day's close.
    """
    import yfinance as yf

    start = target_date - timedelta(days=5)  # buffer for weekends/holidays
    end = target_date + timedelta(days=1)

    try:
        t = yf.Ticker(ticker.upper())
        hist = t.history(start=start.isoformat(), end=end.isoformat())
    except Exception as exc:
        logger.warning("Failed to fetch price for %s on %s: %s", ticker, target_date, exc)
        return None

    if hist.empty:
        return None

    return round(float(hist["Close"].iloc[-1]), 2)


def get_daily_prices(ticker: str, start_date: date, end_date: date) -> dict[str, float]:
    """
    Get daily closing prices for a date range. Returns {date_str: close_price}.
    Used by the simulator to check stops/targets day by day.
    """
    import yfinance as yf

    try:
        t = yf.Ticker(ticker.upper())
        hist = t.history(
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
        )
    except Exception as exc:
        logger.warning("Failed to fetch daily prices for %s: %s", ticker, exc)
        return {}

    if hist.empty:
        return {}

    return {
        row_date.strftime("%Y-%m-%d"): round(float(row["Close"]), 2)
        for row_date, row in hist.iterrows()
    }
