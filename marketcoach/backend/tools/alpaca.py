"""
Alpaca paper-trading wrapper.

Provides paper-trading via alpaca-py SDK: market orders, position management,
and account queries. Exposes an ALPACA_TOOL dict + execute_alpaca_tool() for
use by Claude agents (same pattern as WEB_SEARCH_TOOL).

SAFETY CONSTRAINT: paper_only=True is enforced at this layer.
Live trading requires explicit user opt-in AND a separate confirmation
step in the API — never triggered automatically.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    qty: float
    side: str   # buy | sell
    status: str
    is_paper: bool
    fill_price: Optional[float] = None


class AlpacaClient:
    """
    Wraps Alpaca Trade API for paper and (opt-in) live trading.
    """

    def __init__(self, api_key: str = "", secret_key: str = "", paper: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper  # always True until user explicitly opts in
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
            logger.info("Alpaca client initialised (paper=%s)", self.paper)
        except ImportError:
            logger.warning("alpaca-py not installed — Alpaca features disabled")
        except Exception as exc:
            logger.warning("Alpaca client init failed: %s", exc)

    # ── Read operations ───────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """Return account summary (equity, buying power, etc.)."""
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
        """Return all open positions."""
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
        """Return recent orders (most recent first)."""
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

    # ── Write operations (paper-only guard) ───────────────────────────────────

    def place_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        paper_only: bool = True,
    ) -> OrderResult:
        """
        Place a market order.

        Args:
            paper_only: Must be True unless user has explicitly opted into live trading.
                        This is enforced here AND in the API layer.
        """
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
                "Order submitted: %s %s x%.2f → %s (paper=%s)",
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
            logger.error("place_order failed: %s", exc)
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status=f"error: {exc}",
                is_paper=self.paper,
            )

    def close_position(self, ticker: str) -> dict:
        """Close a specific position by ticker symbol."""
        if self._client is None:
            return {"error": "Alpaca not configured"}

        try:
            order = self._client.close_position(ticker)
            logger.info("Closed position: %s (paper=%s)", ticker, self.paper)
            return {
                "order_id": str(order.id),
                "ticker": order.symbol,
                "side": order.side.value,
                "status": order.status.value,
            }
        except Exception as exc:
            logger.error("close_position(%s) failed: %s", ticker, exc)
            return {"error": str(exc), "ticker": ticker}

    def close_all_positions(self) -> dict:
        """
        Close all open positions. Only allowed on paper accounts.

        This is a destructive operation — the paper_only guard prevents
        accidental liquidation of a live portfolio.
        """
        if not self.paper:
            return {
                "error": "close_all_positions is only allowed on paper accounts. "
                "This guard exists to prevent accidental liquidation of live portfolios."
            }

        if self._client is None:
            return {"error": "Alpaca not configured"}

        try:
            responses = self._client.close_all_positions(cancel_orders=True)
            logger.info(
                "Closed all positions (%d) on paper account", len(responses)
            )
            return {
                "closed": len(responses),
                "paper": True,
            }
        except Exception as exc:
            logger.error("close_all_positions failed: %s", exc)
            return {"error": str(exc)}


# ── Tool definition for Claude agents ────────────────────────────────────────

ALPACA_TOOL: dict = {
    "name": "alpaca_trading",
    "description": (
        "Interact with the Alpaca paper-trading account. View positions, "
        "account balance, place market orders (buy/sell), and close positions. "
        "All operations run against the PAPER account — live trading is never "
        "triggered automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "get_positions",
                    "get_account",
                    "get_order_history",
                    "place_order",
                    "close_position",
                ],
                "description": "The trading action to perform.",
            },
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol (required for place_order, close_position).",
            },
            "qty": {
                "type": "number",
                "description": "Number of shares (required for place_order).",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "Order direction (required for place_order).",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of orders to return (1–100, default 20). Only used with get_order_history.",
                "default": 20,
            },
        },
        "required": ["action"],
    },
}


# Read-only subset — exposed to advisory agents that should never place orders.
# Same tool name so model behaviour is consistent, but the enum is narrowed.
ALPACA_READ_TOOL: dict = {
    "name": "alpaca_account",
    "description": (
        "Read-only view of the Alpaca paper-trading account. Use this to check "
        "current positions, cash/buying power, portfolio value, and recent order "
        "history when reasoning about trade sizing, concentration risk, or "
        "portfolio review. This tool cannot place or cancel orders — recommend "
        "trades in text; the user executes them manually."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get_positions", "get_account", "get_order_history"],
                "description": (
                    "'get_positions' = all open positions with qty, avg entry, "
                    "current price, unrealised P&L. "
                    "'get_account' = equity, buying power, cash, portfolio value. "
                    "'get_order_history' = recent orders (most recent first)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max orders to return for get_order_history (1–100, default 20).",
                "default": 20,
            },
        },
        "required": ["action"],
    },
}


# Read-only action names — anything outside this set is rejected when called
# through the advisory (read-only) path.
_READ_ONLY_ACTIONS = frozenset({"get_positions", "get_account", "get_order_history"})


def execute_alpaca_read_tool(action: str, **kwargs) -> dict:
    """
    Read-only dispatch for advisory agents. Rejects any write action with an
    explicit error so Claude falls back to recommending the trade in text.
    """
    if action not in _READ_ONLY_ACTIONS:
        return {
            "error": (
                f"Action '{action}' is not allowed in read-only mode. This agent "
                "can view account state but cannot place or cancel orders. "
                "Recommend the trade in your response; the user will execute it."
            )
        }
    return execute_alpaca_tool(action, **kwargs)


# Singleton client — initialised once when the module is first used.
# The API layer (FastAPI) should call init_alpaca_client() at startup.
_client_instance: Optional[AlpacaClient] = None


def init_alpaca_client(
    api_key: str, secret_key: str, paper: bool = True
) -> AlpacaClient:
    """Create and cache the module-level AlpacaClient singleton."""
    global _client_instance
    _client_instance = AlpacaClient(api_key=api_key, secret_key=secret_key, paper=paper)
    return _client_instance


def get_alpaca_client() -> Optional[AlpacaClient]:
    """Return the cached AlpacaClient, or None if not initialised."""
    return _client_instance


def execute_alpaca_tool(action: str, **kwargs) -> dict:
    """
    Execute an Alpaca tool action — called by the agentic loop when Claude
    invokes the alpaca_trading tool.

    Returns a JSON-serialisable dict suitable for tool_result content.
    """
    client = get_alpaca_client()
    if client is None:
        return {"error": "Alpaca client not initialised. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."}

    if action == "get_positions":
        positions = client.get_positions()
        return {"positions": positions, "count": len(positions)}

    if action == "get_account":
        return client.get_account()

    if action == "get_order_history":
        limit = int(kwargs.get("limit", 20))
        orders = client.get_order_history(limit=limit)
        return {"orders": orders, "count": len(orders)}

    if action == "place_order":
        ticker = kwargs.get("ticker")
        qty = kwargs.get("qty")
        side = kwargs.get("side")

        if not all([ticker, qty, side]):
            return {"error": "place_order requires ticker, qty, and side"}

        result = client.place_order(
            ticker=ticker, qty=float(qty), side=side, paper_only=True
        )
        return {
            "order_id": result.order_id,
            "ticker": result.ticker,
            "qty": result.qty,
            "side": result.side,
            "status": result.status,
            "is_paper": result.is_paper,
            "fill_price": result.fill_price,
        }

    if action == "close_position":
        ticker = kwargs.get("ticker")
        if not ticker:
            return {"error": "close_position requires ticker"}
        return client.close_position(ticker)

    return {"error": f"Unknown action: {action}"}
