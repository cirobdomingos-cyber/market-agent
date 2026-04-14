"""
AlpacaBroker — BrokerClient implementation for Alpaca's REST API.

Refactored from the original tools/alpaca.py to fit the BrokerClient ABC.
The shape of every method matches the abstract contract; provider-specific
parsing (turning Alpaca SDK objects into vendor-neutral dicts) stays in
this file and never leaks upward.
"""

import logging
from typing import Optional

from backend.brokers.base import BrokerClient, OrderResult

logger = logging.getLogger(__name__)


class AlpacaBroker(BrokerClient):
    """Alpaca Trade API client. Defaults to paper; live requires explicit opt-in."""

    def __init__(self, api_key: str = "", secret_key: str = "", paper: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self._client = None

        if api_key and secret_key:
            self._init_client()

    def _init_client(self) -> None:
        try:
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
            )
            logger.info("Alpaca broker initialised (paper=%s)", self.paper)
        except ImportError:
            logger.warning("alpaca-py not installed — Alpaca features disabled")
        except Exception as exc:
            logger.warning("Alpaca client init failed: %s", exc)

    def is_connected(self) -> bool:
        return self._client is not None

    # ── Read operations ──────────────────────────────────────────────────────

    def get_account(self) -> dict:
        if self._client is None:
            return {"status": "disconnected", "message": "Alpaca not configured"}
        try:
            account = self._client.get_account()
            return {
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "cash": float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "paper": self.paper,
            }
        except Exception as exc:
            logger.error("get_account failed: %s", exc)
            return {"error": str(exc)}

    def get_positions(self) -> list[dict]:
        if self._client is None:
            return []
        try:
            positions = self._client.get_all_positions()
            return [
                {
                    "ticker": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealised_pnl": float(p.unrealized_pl),
                    "unrealised_pnl_pct": float(p.unrealized_plpc) * 100,
                }
                for p in positions
            ]
        except Exception as exc:
            logger.error("get_positions failed: %s", exc)
            return []

    def get_order_history(self, limit: int = 20) -> list[dict]:
        if self._client is None:
            return []
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=min(max(1, limit), 100),
            )
            orders = self._client.get_orders(filter=request)
            return [
                {
                    "order_id": str(order.id),
                    "ticker": order.symbol,
                    "qty": float(order.qty) if order.qty else None,
                    "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
                    "side": order.side.value,
                    "type": order.type.value,
                    "status": order.status.value,
                    "filled_avg_price": (
                        float(order.filled_avg_price)
                        if order.filled_avg_price
                        else None
                    ),
                    "submitted_at": str(order.submitted_at),
                    "filled_at": str(order.filled_at) if order.filled_at else None,
                }
                for order in orders
            ]
        except Exception as exc:
            logger.error("get_order_history failed: %s", exc)
            return []

    # ── Write operations (paper-only guard) ──────────────────────────────────

    def place_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        paper_only: bool = True,
    ) -> OrderResult:
        if not paper_only and not self.paper:
            raise ValueError(
                "Live trading requires explicit user confirmation. "
                "Set paper_only=True or use the paper account."
            )
        if self._client is None:
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status="error_disconnected",
                is_paper=paper_only,
            )

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(request)

            logger.info(
                "Alpaca order submitted: %s %s x%.2f → %s (paper=%s)",
                side.upper(), ticker, qty, order.status.value, self.paper,
            )

            return OrderResult(
                order_id=str(order.id),
                ticker=order.symbol,
                qty=float(order.qty) if order.qty else qty,
                side=order.side.value,
                status=order.status.value,
                is_paper=self.paper,
                fill_price=(
                    float(order.filled_avg_price)
                    if order.filled_avg_price
                    else None
                ),
            )
        except Exception as exc:
            logger.error("Alpaca place_order failed: %s", exc)
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status=f"error: {exc}",
                is_paper=self.paper,
            )

    def close_position(self, ticker: str) -> dict:
        if self._client is None:
            return {"error": "Alpaca not configured"}
        try:
            order = self._client.close_position(ticker)
            logger.info("Alpaca closed position: %s (paper=%s)", ticker, self.paper)
            return {
                "order_id": str(order.id),
                "ticker": order.symbol,
                "side": order.side.value,
                "status": order.status.value,
            }
        except Exception as exc:
            logger.error("Alpaca close_position(%s) failed: %s", ticker, exc)
            return {"error": str(exc), "ticker": ticker}
