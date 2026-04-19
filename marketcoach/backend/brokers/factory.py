"""
Broker factory + module-level singleton.

`init_broker()` is called once at startup from main.py's lifespan with the
chosen provider name. `get_broker()` returns the cached singleton and is
called from everywhere else (orchestrator, /orders/confirm, agent tools).

Selection rules:
  - settings.broker_provider == "ibkr"   → IBKRBroker (default for unknown)
  - settings.broker_provider == "none"   → no broker, get_broker() returns None
  - settings.broker_provider == "alpaca" → ValueError (removed, see history)
  - Anything else                        → log warning, fall back to IBKR

The "none" provider exists so the backend can run in environments that
cannot reach a broker at all (e.g. a cloud-hosted analysis service with
IB Gateway staying local). Every call site already short-circuits on
get_broker() is None, so the analysis features keep working while the
trading endpoints return clean "disconnected" responses.

Historical: "alpaca" was the default before IBKR was added. It was
removed because Alpaca stopped onboarding Brazilian residents for live
trading and the integration never exercised its live path for this
project. We raise a clear ValueError if anyone still has BROKER_PROVIDER=
alpaca in their .env so the failure points at the fix (switch to ibkr or
none) rather than silently falling back.

We deliberately do NOT import the broker classes at module load so that
ib_insync doesn't have to be installed when BROKER_PROVIDER=none.
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
        provider: "ibkr" | "none"
        **kwargs: provider-specific config — passed straight through to the
                  concrete __init__. See IBKRBroker.

    Returns the cached instance (or None for "none" provider).
    """
    global _broker_instance

    provider = (provider or "ibkr").lower().strip()

    if provider == "none":
        _broker_instance = None
        logger.info(
            "Broker provider='none' — broker features disabled. "
            "Trading endpoints will return 'disconnected'; analysis features work normally."
        )
        return None

    if provider == "alpaca":
        raise ValueError(
            "BROKER_PROVIDER=alpaca was removed. The integration never "
            "exercised its live path because Alpaca stopped onboarding "
            "Brazilian residents. Set BROKER_PROVIDER=ibkr to use "
            "Interactive Brokers, or BROKER_PROVIDER=none to disable "
            "broker features entirely."
        )

    if provider == "ibkr":
        from backend.brokers.ibkr import IBKRBroker
        _broker_instance = IBKRBroker(**kwargs)
        return _broker_instance

    logger.warning(
        "Unknown broker provider '%s'. Falling back to IBKR.", provider
    )
    from backend.brokers.ibkr import IBKRBroker
    _broker_instance = IBKRBroker(**kwargs)
    return _broker_instance


def get_broker() -> Optional[BrokerClient]:
    """Return the cached broker singleton, or None if not initialised."""
    return _broker_instance


def reset_broker() -> None:
    """Test helper — clear the singleton between tests."""
    global _broker_instance
    _broker_instance = None
