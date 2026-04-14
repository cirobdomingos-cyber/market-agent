"""
Broker account tool — read-only Anthropic tool definition.

Exposes read access to the active broker (Alpaca, IBKR, future LocalBroker)
to the TradingAdvisor agent. Renamed from `alpaca_account` → `broker_account`
when we added the broker abstraction so the tool name doesn't lie about which
provider it talks to.

The agent NEVER gets the write subset. If we ever want agent-side execution
it would be a separate tool with its own per-call confirmation gate.
"""

import logging

from backend.brokers.factory import get_broker

logger = logging.getLogger(__name__)


BROKER_READ_TOOL: dict = {
    "name": "broker_account",
    "description": (
        "Read-only view of the trading account. Use this to check current "
        "positions, cash/buying power, portfolio value, and recent order "
        "history when reasoning about trade sizing, concentration risk, or "
        "portfolio review. This tool cannot place or cancel orders — "
        "recommend trades in text and the user clicks Execute."
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


# Hard whitelist: anything outside this set is rejected even if the schema
# enum is bypassed somehow. Defence in depth at the dispatch layer.
_READ_ONLY_ACTIONS = frozenset({"get_positions", "get_account", "get_order_history"})


def execute_broker_read_tool(action: str, **kwargs) -> dict:
    """
    Read-only dispatch. Routes the agent's request through the active broker.
    Rejects any action outside the read-only whitelist with an explicit error
    so Claude falls back to recommending the trade in text.
    """
    if action not in _READ_ONLY_ACTIONS:
        return {
            "error": (
                f"Action '{action}' is not allowed in read-only mode. This agent "
                "can view account state but cannot place or cancel orders. "
                "Recommend the trade in your response and the user will execute it."
            )
        }

    broker = get_broker()
    if broker is None:
        return {
            "error": (
                "Broker not initialised. Set ALPACA_API_KEY/ALPACA_SECRET_KEY "
                "for Alpaca, or start IB Gateway and set BROKER_PROVIDER=ibkr."
            )
        }

    if action == "get_positions":
        positions = broker.get_positions()
        return {"positions": positions, "count": len(positions)}

    if action == "get_account":
        return broker.get_account()

    if action == "get_order_history":
        limit = int(kwargs.get("limit", 20))
        orders = broker.get_order_history(limit=limit)
        return {"orders": orders, "count": len(orders)}

    return {"error": f"Unknown action: {action}"}
