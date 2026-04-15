"""
Broker factory + module-level singleton.

`init_broker()` is called once at startup from main.py's lifespan with the
chosen provider name. `get_broker()` returns the cached singleton and is
called from everywhere else (orchestrator, /orders/confirm, agent tools).

Selection rules:
  - settings.broker_provider == "alpaca" → AlpacaBroker (default)
  - settings.broker_provider == "ibkr"   → IBKRBroker
  - settings.broker_provider == "none"   → no broker, get_broker() returns None
  - Anything else                        → log warning, fall back to Alpaca

The "none" provider exists so the backend can run in environments that
cannot reach a broker at all (e.g. a cloud-hosted analysis service with
IB Gateway staying local). Every call site already short-circuits on
get_broker() is None, so the analysis features keep working while the
trading endpoints return clean "disconnected" responses.

We deliberately do NOT import the broker classes at module load so that
neither alpaca-py nor ib_insync needs to be installed when the other
provider is selected.
"""

import logging
from typing import Optional

from backend.brokers.base import BrokerClient

logger = logging.getLogger(__name__)

_broker_instance: Optional[BrokerClient] = None


def init_broker(provider: str, **kwargs) -> Optional[BrokerClient]:
    """
    Build the broker singleton for the chosen provider.

    Args:
        provider: "alpaca" | "ibkr"
        **kwargs: provider-specific config — passed straight through to the
                  concrete __init__. See AlpacaBroker / IBKRBroker.

    Returns the cached instance (or None if construction failed entirely).
    """
    global _broker_instance

    provider = (provider or "alpaca").lower().strip()

    if provider == "none":
        _broker_instance = None
        logger.info(
            "Broker provider='none' — broker features disabled. "
            "Trading endpoints will return 'disconnected'; analysis features work normally."
        )
        return None

    if provider == "alpaca":
        from backend.brokers.alpaca import AlpacaBroker
        _broker_instance = AlpacaBroker(**kwargs)
        return _broker_instance

    if provider == "ibkr":
        from backend.brokers.ibkr import IBKRBroker
        _broker_instance = IBKRBroker(**kwargs)
        return _broker_instance

    logger.warning(
        "Unknown broker provider '%s'. Falling back to Alpaca.", provider
    )
    from backend.brokers.alpaca import AlpacaBroker
    _broker_instance = AlpacaBroker(**kwargs)
    return _broker_instance


def get_broker() -> Optional[BrokerClient]:
    """Return the cached broker singleton, or None if not initialised."""
    return _broker_instance


def reset_broker() -> None:
    """Test helper — clear the singleton between tests."""
    global _broker_instance
    _broker_instance = None
