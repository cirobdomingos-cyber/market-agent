"""
Broker abstraction layer.

MarketCoach is broker-agnostic at the orchestration layer. This package
defines a single `BrokerClient` ABC that each concrete broker (Alpaca,
IBKR, future LocalBroker for paper-only simulation) implements identically.

Why an abstraction:
  - Alpaca stopped accepting Brazilian residents for live accounts. Rather
    than rewrite the whole stack, we needed a way to swap the broker without
    touching the agents, the API endpoints, or the frontend.
  - IBKR (Interactive Brokers) accepts Brazilian residents and has the only
    serious API in international retail brokerage — but its client is much
    clunkier than Alpaca's REST API. The abstraction hides those differences.
  - A future virtual-portfolio broker can drop in here too if we ever want
    a fully self-hosted simulation path.

Selection happens at startup via settings.broker_provider ("alpaca" | "ibkr").
The factory `init_broker()` is called from main.py's lifespan; everything
else in the codebase calls `get_broker()` to retrieve the singleton.
"""

from backend.brokers.base import BrokerClient, OrderResult
from backend.brokers.factory import get_broker, init_broker

__all__ = [
    "BrokerClient",
    "OrderResult",
    "get_broker",
    "init_broker",
]
