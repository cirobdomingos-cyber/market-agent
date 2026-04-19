"""
Broker abstraction layer.

MarketCoach is broker-agnostic at the orchestration layer. This package
defines a single `BrokerClient` ABC that concrete brokers implement.

Currently supported providers:
  - "ibkr" — Interactive Brokers via ib_insync + local IB Gateway. The
    active broker for live trading (IBKR accepts Brazilian residents
    where most US retail brokers don't).
  - "none" — explicit opt-out. Used by cloud-hosted analysis services
    that can't reach a local broker. Trading endpoints return 503;
    analysis features are unaffected.

Historical note: "alpaca" was the original default and was removed in
the refactor/remove-alpaca branch. Alpaca stopped onboarding Brazilian
residents for live trading, so the integration never exercised its
live path for this project and became dead code after IBKR was wired
up. The abstraction still makes sense — a future LocalBroker for pure
simulation, or a second live broker on a different market, would drop
in under this same interface.

Selection happens at startup via settings.broker_provider. The factory
`init_broker()` is called from main.py's lifespan; everything else in
the codebase calls `get_broker()` to retrieve the singleton.
"""

from backend.brokers.base import BrokerClient, OrderResult
from backend.brokers.factory import get_broker, init_broker

__all__ = [
    "BrokerClient",
    "OrderResult",
    "get_broker",
    "init_broker",
]
