"""
Pydantic schemas for backtest configuration and results.

These are pure data contracts — no DB dependency. The engine produces these,
the API serializes them, the frontend renders them.
"""

from datetime import date, datetime
from pydantic import BaseModel, Field


class BacktestConfig(BaseModel):
    """User-provided configuration for a backtest run."""

    start_date: date = Field(description="First date of the simulation window")
    end_date: date = Field(description="Last date of the simulation window (defaults to yesterday)")
    initial_capital: float = Field(default=100_000.0, ge=1_000, le=10_000_000)
    watchlist: list[str] = Field(
        default=["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META", "SPY", "QQQ"],
        min_length=1,
        max_length=20,
    )
    decision_interval_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Days between decision points (when the AI re-evaluates)",
    )
    risk_tolerance: str = Field(
        default="moderate",
        pattern=r"^(conservative|moderate|aggressive)$",
    )


class BacktestTrade(BaseModel):
    """One simulated trade within a backtest."""

    ticker: str
    direction: str          # long | short
    entry_date: str         # ISO date
    entry_price: float
    exit_date: str | None   # ISO date, None if still open at backtest end
    exit_price: float | None
    exit_reason: str        # stop_loss | take_profit | expired | end_of_backtest
    stop_loss: float
    take_profit: float
    position_size_pct: float  # % of portfolio allocated
    position_value: float     # $ allocated
    pnl_dollars: float        # realized P&L
    pnl_percent: float        # % return on this trade
    confidence: float
    thesis_direction: str     # bullish | bearish
    thesis_reasoning: str


class BacktestMetrics(BaseModel):
    """Aggregate performance metrics for a completed backtest."""

    initial_capital: float
    final_equity: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    best_trade_pct: float
    worst_trade_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float | None  # None if not enough data points
    profit_factor: float | None  # gross profit / gross loss


class EquityPoint(BaseModel):
    """One point on the equity curve."""
    date: str  # ISO date
    equity: float
    drawdown_pct: float  # current drawdown from peak


class BacktestResult(BaseModel):
    """Complete output of a backtest run."""

    id: str
    config: BacktestConfig
    metrics: BacktestMetrics
    equity_curve: list[EquityPoint]
    trades: list[BacktestTrade]
    decision_points: int  # how many times the AI made decisions
    total_theses_generated: int
    started_at: str   # ISO datetime
    completed_at: str  # ISO datetime
    status: str = "completed"  # completed | failed
    error: str | None = None
