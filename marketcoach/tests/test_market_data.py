"""
Tests for the market_data tool — focused on the suggest_bracket action
added for ATR-based stop suggestions.

The other actions (quote, technicals, fundamentals, price_history,
compare) are exercised indirectly through test_api.py::TestMarketDataRoutes
and aren't duplicated here; this file concentrates on the bracket math
that the advisor now uses as its stop-placement anchor.
"""

from unittest.mock import patch

import pandas as pd
import pytest

from backend.tools.market_data import execute_market_data


def _fake_hist(rows: list[dict]) -> pd.DataFrame:
    """
    Build a minimal OHLC DataFrame the way yfinance returns it, so
    _compute_atr has something to work with. Provide >= 15 rows for a
    valid ATR-14.
    """
    return pd.DataFrame(rows)


def _yfinance_ticker_mock(atr_rows: list[dict]):
    """
    Patches yfinance's Ticker().history() to return the given rows.
    Returns the mock so tests can verify call args if needed.
    """
    from unittest.mock import MagicMock

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _fake_hist(atr_rows)
    return mock_ticker


# Build 15 rows of OHLC data where each bar has a predictable True Range.
# TR = max(H-L, |H-prev_close|, |L-prev_close|). With H-L = 10 every day
# and no gaps, every TR = 10, so ATR-14 = 10 exactly.
_FLAT_ATR_ROWS = [
    {"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 100.0}
    for _ in range(15)
]


class TestSuggestBracketHappyPath:
    """Stop + target math under a known ATR series."""

    def test_swing_horizon_default(self):
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="NVDA",
                entry_price=500.0,
            )
        assert "error" not in result
        # ATR-14 on the flat series = 10.0
        # Swing multiplier = 2.0 → risk per share = 20.0
        assert result["atr_14"] == pytest.approx(10.0, abs=0.01)
        assert result["atr_multiplier"] == 2.0
        assert result["risk_per_share"] == pytest.approx(20.0, abs=0.01)
        # Stop = 500 - 20 = 480
        assert result["stop_loss"] == pytest.approx(480.0, abs=0.01)
        # Target 1 = 500 + 2*20 = 540 (2:1 R:R)
        assert result["target_1"] == pytest.approx(540.0, abs=0.01)
        # Target 2 = 500 + 3*20 = 560 (3:1)
        assert result["target_2"] == pytest.approx(560.0, abs=0.01)
        # R:R fields
        assert result["rr_target_1"] == 2.0
        assert result["rr_target_2"] == 3.0
        assert result["ticker"] == "NVDA"
        assert result["horizon"] == "swing"

    def test_day_horizon_uses_tighter_multiplier(self):
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="NVDA",
                entry_price=500.0,
                horizon="day",
            )
        assert result["atr_multiplier"] == 1.0
        assert result["risk_per_share"] == pytest.approx(10.0, abs=0.01)
        # Stop = 500 - 10 = 490
        assert result["stop_loss"] == pytest.approx(490.0, abs=0.01)

    def test_position_horizon_uses_wider_multiplier(self):
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="NVDA",
                entry_price=500.0,
                horizon="position",
            )
        assert result["atr_multiplier"] == 3.0
        assert result["risk_per_share"] == pytest.approx(30.0, abs=0.01)
        # Stop = 500 - 30 = 470
        assert result["stop_loss"] == pytest.approx(470.0, abs=0.01)

    def test_reasoning_includes_atr_multiplier_and_adjustment_note(self):
        """
        The reasoning string is what the advisor quotes in its response.
        It must explicitly tell the advisor to ADJUST from these anchors
        so the stop ends up at real support, not the exact ATR line.
        """
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="NVDA",
                entry_price=500.0,
            )
        reasoning = result["reasoning"]
        assert "ATR-14" in reasoning
        assert "2.0" in reasoning or "2×" in reasoning or "2.0×" in reasoning
        assert "ADJUST" in reasoning
        assert "support" in reasoning
        assert "resistance" in reasoning

    def test_ticker_is_uppercased(self):
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="nvda",
                entry_price=500.0,
            )
        assert result["ticker"] == "NVDA"


class TestSuggestBracketValidation:
    """Inputs that can't produce a meaningful answer must fail loudly."""

    def test_missing_entry_price_returns_error(self):
        result = execute_market_data(
            "suggest_bracket",
            ticker="NVDA",
            # entry_price intentionally omitted
        )
        assert "error" in result
        assert "entry_price" in result["error"]

    def test_zero_entry_price_returns_error(self):
        result = execute_market_data(
            "suggest_bracket",
            ticker="NVDA",
            entry_price=0.0,
        )
        assert "error" in result
        assert "positive" in result["error"]

    def test_negative_entry_price_returns_error(self):
        result = execute_market_data(
            "suggest_bracket",
            ticker="NVDA",
            entry_price=-10.0,
        )
        assert "error" in result

    def test_unknown_horizon_returns_error(self):
        result = execute_market_data(
            "suggest_bracket",
            ticker="NVDA",
            entry_price=500.0,
            horizon="scalp",  # not in the known set
        )
        assert "error" in result
        assert "horizon" in result["error"].lower()

    def test_missing_ticker_returns_error(self):
        result = execute_market_data(
            "suggest_bracket",
            ticker=None,
            entry_price=500.0,
        )
        assert "error" in result


class TestSuggestBracketDegradation:
    """yfinance failures or unusable data must return a clear error with
    a suggested fallback, not a silent bad number."""

    def test_empty_history_returns_fallback_hint(self):
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock([])):  # empty DataFrame
            result = execute_market_data(
                "suggest_bracket",
                ticker="NEWIPO",
                entry_price=50.0,
            )
        assert "error" in result
        # Caller gets a clear "use % fallback" hint
        assert "suggested_fallback" in result

    def test_thin_history_returns_error(self):
        """Fewer than 15 rows means ATR-14 can't compute."""
        thin = _FLAT_ATR_ROWS[:5]
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(thin)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="NEWIPO",
                entry_price=50.0,
            )
        assert "error" in result

    def test_nonpositive_stop_on_penny_entry_refuses(self):
        """
        If the ATR is so large relative to entry that the stop would
        land at or below zero, refuse rather than emit nonsense.
        """
        # ATR=10 × 2 = risk_per_share 20. Entry $5 would give stop -$15.
        with patch("backend.tools.market_data._get_ticker",
                   return_value=_yfinance_ticker_mock(_FLAT_ATR_ROWS)):
            result = execute_market_data(
                "suggest_bracket",
                ticker="JUNK",
                entry_price=5.0,
            )
        assert "error" in result
        assert "non-positive" in result["error"].lower() or "penny" in result["error"].lower()


class TestSuggestBracketInToolDefinition:
    """Make sure the tool schema the advisor sees actually includes the
    new action + the new params, so Claude can discover it."""

    def test_suggest_bracket_in_enum(self):
        from backend.tools.market_data import MARKET_DATA_TOOL
        action_schema = MARKET_DATA_TOOL["input_schema"]["properties"]["action"]
        assert "suggest_bracket" in action_schema["enum"]

    def test_entry_price_and_horizon_in_properties(self):
        from backend.tools.market_data import MARKET_DATA_TOOL
        props = MARKET_DATA_TOOL["input_schema"]["properties"]
        assert "entry_price" in props
        assert "horizon" in props
        assert props["horizon"]["enum"] == ["day", "swing", "position"]
        # Default should be the swing horizon (matches the user's actual
        # trading style)
        assert props["horizon"]["default"] == "swing"
