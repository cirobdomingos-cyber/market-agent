"""
Portfolio simulator — manages virtual positions and equity tracking.

This is a pure-math module with no API calls. It takes trade ideas and
daily prices, then simulates position management: entry, stop-loss hits,
take-profit hits, and expiry.

Interview angle:
  This is an event-driven simulation — the same pattern used in production
  backtesting frameworks like Backtrader and Zipline. Each day is a "tick"
  that checks all open positions against their rules. The key insight is
  that you process stops BEFORE targets on the same bar, because in a real
  market a gap through your stop would fill at the stop, not the target.
  Interviewers ask about this because it reveals whether you understand
  order execution vs. signal generation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

from backend.backtest.schemas import BacktestMetrics, BacktestTrade, EquityPoint

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    """A position currently held in the simulated portfolio."""

    ticker: str
    direction: str           # long | short
    entry_date: str          # ISO date
    entry_price: float
    stop_loss: float
    take_profit: float
    position_value: float    # $ allocated at entry
    position_size_pct: float
    confidence: float
    thesis_direction: str
    thesis_reasoning: str
    expires_on: str | None = None  # ISO date — auto-close if not hit


@dataclass
class PortfolioSimulator:
    """
    Simulates a portfolio over time with daily price checks.

    Usage:
        sim = PortfolioSimulator(initial_capital=100_000)
        sim.open_position(...)       # when AI says to buy
        sim.process_day(date, prices)  # for each trading day
        result = sim.get_results()    # at end of backtest
    """

    initial_capital: float
    cash: float = 0.0
    positions: list[OpenPosition] = field(default_factory=list)
    closed_trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    peak_equity: float = 0.0

    def __post_init__(self):
        self.cash = self.initial_capital
        self.peak_equity = self.initial_capital

    def open_position(
        self,
        ticker: str,
        direction: str,
        entry_date: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size_pct: float,
        confidence: float,
        thesis_direction: str,
        thesis_reasoning: str,
        expires_on: str | None = None,
    ) -> bool:
        """
        Open a new position if we have enough cash.
        Returns True if opened, False if insufficient funds.
        """
        current_equity = self._calculate_equity({})
        position_value = current_equity * position_size_pct

        if position_value > self.cash:
            # Scale down to available cash
            position_value = self.cash * 0.95  # keep 5% cash buffer
            if position_value <= 0:
                logger.debug("Skipping %s — no cash available", ticker)
                return False

        # Don't open duplicate positions
        if any(p.ticker == ticker for p in self.positions):
            logger.debug("Skipping %s — already have a position", ticker)
            return False

        self.cash -= position_value

        self.positions.append(OpenPosition(
            ticker=ticker,
            direction=direction,
            entry_date=entry_date,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_value=position_value,
            position_size_pct=position_size_pct,
            confidence=confidence,
            thesis_direction=thesis_direction,
            thesis_reasoning=thesis_reasoning,
            expires_on=expires_on,
        ))

        logger.debug(
            "Opened %s %s @ $%.2f (value=$%.2f, stop=$%.2f, target=$%.2f)",
            direction, ticker, entry_price, position_value, stop_loss, take_profit,
        )
        return True

    def process_day(self, current_date: str, prices: dict[str, float]) -> None:
        """
        Process one trading day: check stops, targets, and expiry for all
        open positions. Record equity point.

        prices: {ticker: closing_price}
        """
        to_close: list[tuple[OpenPosition, float, str]] = []

        for pos in self.positions:
            price = prices.get(pos.ticker)
            if price is None:
                continue  # no data for this ticker today (holiday, etc.)

            # Check stop-loss first (conservative — stops before targets)
            if pos.direction == "long" and price <= pos.stop_loss:
                to_close.append((pos, pos.stop_loss, "stop_loss"))
            elif pos.direction == "short" and price >= pos.stop_loss:
                to_close.append((pos, pos.stop_loss, "stop_loss"))
            # Check take-profit
            elif pos.direction == "long" and price >= pos.take_profit:
                to_close.append((pos, pos.take_profit, "take_profit"))
            elif pos.direction == "short" and price <= pos.take_profit:
                to_close.append((pos, pos.take_profit, "take_profit"))
            # Check expiry
            elif pos.expires_on and current_date >= pos.expires_on:
                to_close.append((pos, price, "expired"))

        for pos, exit_price, reason in to_close:
            self._close_position(pos, current_date, exit_price, reason)

        # Record equity
        equity = self._calculate_equity(prices)
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = ((self.peak_equity - equity) / self.peak_equity * 100) if self.peak_equity > 0 else 0.0

        self.equity_curve.append(EquityPoint(
            date=current_date,
            equity=round(equity, 2),
            drawdown_pct=round(drawdown, 2),
        ))

    def close_all_positions(self, current_date: str, prices: dict[str, float]) -> None:
        """Force-close all open positions at end of backtest."""
        for pos in list(self.positions):
            price = prices.get(pos.ticker, pos.entry_price)
            self._close_position(pos, current_date, price, "end_of_backtest")

    def _close_position(
        self, pos: OpenPosition, exit_date: str, exit_price: float, reason: str
    ) -> None:
        """Close a position and record the trade."""
        # Calculate P&L
        if pos.direction == "long":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
        else:  # short
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

        pnl_dollars = pos.position_value * pnl_pct

        # Return capital + P&L to cash
        self.cash += pos.position_value + pnl_dollars

        self.closed_trades.append(BacktestTrade(
            ticker=pos.ticker,
            direction=pos.direction,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            exit_date=exit_date,
            exit_price=round(exit_price, 2),
            exit_reason=reason,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            position_size_pct=pos.position_size_pct,
            position_value=round(pos.position_value, 2),
            pnl_dollars=round(pnl_dollars, 2),
            pnl_percent=round(pnl_pct * 100, 2),
            confidence=pos.confidence,
            thesis_direction=pos.thesis_direction,
            thesis_reasoning=pos.thesis_reasoning,
        ))

        self.positions.remove(pos)

        logger.debug(
            "Closed %s %s @ $%.2f (%s) P&L=$%.2f (%.1f%%)",
            pos.direction, pos.ticker, exit_price, reason, pnl_dollars, pnl_pct * 100,
        )

    def _calculate_equity(self, prices: dict[str, float]) -> float:
        """Total equity = cash + mark-to-market value of open positions."""
        positions_value = 0.0
        for pos in self.positions:
            current_price = prices.get(pos.ticker)
            if current_price is None:
                # No price available — use entry price (conservative)
                positions_value += pos.position_value
            else:
                if pos.direction == "long":
                    pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                else:
                    pnl_pct = (pos.entry_price - current_price) / pos.entry_price
                positions_value += pos.position_value * (1 + pnl_pct)

        return self.cash + positions_value

    def get_metrics(self) -> BacktestMetrics:
        """Compute aggregate metrics from closed trades and equity curve."""
        trades = self.closed_trades
        final_equity = self.equity_curve[-1].equity if self.equity_curve else self.initial_capital

        total = len(trades)
        winners = [t for t in trades if t.pnl_dollars > 0]
        losers = [t for t in trades if t.pnl_dollars <= 0]

        win_pcts = [t.pnl_percent for t in winners]
        loss_pcts = [t.pnl_percent for t in losers]

        # Max drawdown from equity curve
        max_dd = max((ep.drawdown_pct for ep in self.equity_curve), default=0.0)

        # Sharpe ratio (annualized, using daily equity returns)
        sharpe = self._calculate_sharpe()

        # Profit factor
        gross_profit = sum(t.pnl_dollars for t in winners)
        gross_loss = abs(sum(t.pnl_dollars for t in losers))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

        all_pcts = [t.pnl_percent for t in trades]

        return BacktestMetrics(
            initial_capital=self.initial_capital,
            final_equity=round(final_equity, 2),
            total_return_pct=round((final_equity - self.initial_capital) / self.initial_capital * 100, 2),
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate_pct=round(len(winners) / total * 100, 1) if total > 0 else 0.0,
            avg_win_pct=round(sum(win_pcts) / len(win_pcts), 2) if win_pcts else 0.0,
            avg_loss_pct=round(sum(loss_pcts) / len(loss_pcts), 2) if loss_pcts else 0.0,
            best_trade_pct=round(max(all_pcts), 2) if all_pcts else 0.0,
            worst_trade_pct=round(min(all_pcts), 2) if all_pcts else 0.0,
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
        )

    def _calculate_sharpe(self, risk_free_rate: float = 0.05) -> float | None:
        """
        Annualized Sharpe ratio from daily equity returns.

        Sharpe = (mean_daily_return - risk_free_daily) / std_daily_return * sqrt(252)

        Returns None if fewer than 5 data points.
        """
        if len(self.equity_curve) < 5:
            return None

        equities = [ep.equity for ep in self.equity_curve]
        daily_returns = []
        for i in range(1, len(equities)):
            if equities[i - 1] > 0:
                daily_returns.append((equities[i] - equities[i - 1]) / equities[i - 1])

        if len(daily_returns) < 5:
            return None

        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
        std_return = math.sqrt(variance)

        if std_return == 0:
            return None

        daily_rf = risk_free_rate / 252
        sharpe = (mean_return - daily_rf) / std_return * math.sqrt(252)
        return round(sharpe, 2)
