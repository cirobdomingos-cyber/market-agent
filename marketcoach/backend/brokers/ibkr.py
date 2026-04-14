"""
IBKRBroker — BrokerClient implementation for Interactive Brokers via ib_insync.

Why IBKR:
  Alpaca stopped accepting Brazilian residents for live accounts. IBKR is
  the only serious retail broker that accepts Brazilian residents AND has
  a real API. This implementation lets MarketCoach swap brokers at startup
  with a single settings flag — no agent or endpoint code changes needed.

How IBKR is different from Alpaca:
  - IBKR talks via a TCP socket to a local "IB Gateway" or TWS process,
    NOT over HTTPS. You must run IB Gateway (a Java app) on the same
    machine as this backend, logged into your account.
  - The official ibapi library is callback-based and painful. We use
    ib_insync, the community wrapper that gives us a clean sync/async API.
  - Paper vs live is decided by which IBKR account you log Gateway into.
    Same code, different login. Default paper port is 7497, live is 7496.
  - Order objects, Position objects, and AccountSummary use IBKR-specific
    field names. Everything is normalised to vendor-neutral dicts here so
    the rest of the codebase doesn't notice.

Setup the user does (one-time, after IBKR account is open):
  1. Install IB Gateway from interactivebrokers.com
  2. Log in with paper credentials (you get them automatically with a
     funded Pro account) on port 7497, OR live credentials on port 7496
  3. Enable "ActiveX and Socket Clients" in Gateway → Configure → API
  4. Add 127.0.0.1 to "Trusted IPs"
  5. pip install ib_insync
  6. Set IBKR_HOST=127.0.0.1, IBKR_PORT=7497 (paper) or 7496 (live),
     IBKR_CLIENT_ID=1 in .env
  7. Set BROKER_PROVIDER=ibkr in .env
  8. Restart MarketCoach

The ib_insync import is deferred until first use so the package isn't
required at startup — users who stay on Alpaca don't need to install it.
"""

import asyncio
import logging
import threading
from typing import Optional

from backend.brokers.base import BrokerClient, OrderResult

logger = logging.getLogger(__name__)


# ── ib_insync import at module load time ────────────────────────────────────
#
# The eventkit library (ib_insync's dependency) calls asyncio.get_event_loop()
# at import time to cache a "main" loop. That call works on the main thread
# (where FastAPI's lifespan runs) but fails with "no current event loop in
# thread" when triggered lazily from a FastAPI worker thread.
#
# Fix: import ib_insync once at module load time. This module is imported
# during init_broker("ibkr", ...) which runs inside the async lifespan on
# the main thread, so get_event_loop() finds a real loop.
#
# We still tolerate ib_insync being missing (ImportError → set sentinel)
# so that tests and the Alpaca-only path don't need the package installed.

try:
    from ib_insync import IB as _IB
    from ib_insync import Stock as _Stock
    from ib_insync import MarketOrder as _MarketOrder
    _IBKR_AVAILABLE = True
except ImportError:  # pragma: no cover — graceful degradation
    _IB = None
    _Stock = None
    _MarketOrder = None
    _IBKR_AVAILABLE = False
    logger.debug("ib_insync not installed — IBKR broker will refuse connections")


def _ensure_thread_event_loop() -> None:
    """
    FastAPI sync endpoints run in worker threads with no default event loop.
    ib_insync's sync shims (ib.connect, ib.placeOrder, etc.) need a loop in
    the current thread to run the underlying async operations on. Create a
    fresh loop for the thread if there isn't one — safe no-op when there is.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        # "There is no current event loop in thread '...'"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


class IBKRBroker(BrokerClient):
    """
    Interactive Brokers client via ib_insync.

    The connection is opened lazily on the first call so:
      - Importing this module never blocks on a network round-trip
      - The module loads even when ib_insync isn't installed (the import
        only fires when you actually try to use it)
      - Tests can construct an instance and stub _ib without installing
        the real package
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        paper: bool = True,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        self._ib = None  # lazy-initialised IB instance from ib_insync

    def _ensure_connected(self) -> bool:
        """Connect to IB Gateway on first use. Returns False on any failure."""
        if self._ib is not None and self._ib.isConnected():
            return True

        if not _IBKR_AVAILABLE:
            logger.warning(
                "ib_insync not installed — install with `pip install ib_insync` "
                "and start IB Gateway to enable IBKR broker"
            )
            return False

        # FastAPI sync endpoints run in worker threads with no default event
        # loop. ib_insync needs one to run its async internals on.
        _ensure_thread_event_loop()

        try:
            ib = _IB()
            ib.connect(self.host, self.port, clientId=self.client_id, timeout=5)
            self._ib = ib
            logger.info(
                "IBKR broker connected to %s:%d clientId=%d (paper=%s)",
                self.host, self.port, self.client_id, self.paper,
            )
            return True
        except Exception as exc:
            logger.warning("IBKR connection failed: %s", exc)
            return False

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # ── Read operations ──────────────────────────────────────────────────────

    def get_account(self) -> dict:
        if not self._ensure_connected():
            return {
                "status": "disconnected",
                "message": (
                    "IBKR not reachable. Start IB Gateway, log in, enable "
                    "API access, and verify host/port settings."
                ),
            }

        try:
            # accountSummary returns a list of AccountValue objects keyed by tag
            summary = self._ib.accountSummary()
            tags = {row.tag: row for row in summary}

            def _f(tag: str, default: float = 0.0) -> float:
                row = tags.get(tag)
                if row is None:
                    return default
                try:
                    return float(row.value)
                except (TypeError, ValueError):
                    return default

            return {
                # Map IBKR account-summary tags to the vendor-neutral schema
                "equity": _f("NetLiquidation"),
                "buying_power": _f("BuyingPower"),
                "cash": _f("TotalCashValue"),
                "portfolio_value": _f("NetLiquidation"),
                "paper": self.paper,
            }
        except Exception as exc:
            logger.error("IBKR get_account failed: %s", exc)
            return {"error": str(exc)}

    def get_positions(self) -> list[dict]:
        if not self._ensure_connected():
            return []

        try:
            positions = self._ib.positions()
            results = []
            for p in positions:
                qty = float(p.position)
                avg_entry = float(p.avgCost)
                # Current price requires a market-data subscription — best-effort
                current = None
                try:
                    ticker = self._ib.reqMktData(p.contract, snapshot=True)
                    self._ib.sleep(0.5)  # let the snapshot arrive
                    if ticker.last and ticker.last == ticker.last:  # not NaN
                        current = float(ticker.last)
                except Exception:
                    pass

                pnl = (current - avg_entry) * qty if current else None
                pnl_pct = (
                    ((current - avg_entry) / avg_entry) * 100
                    if current and avg_entry
                    else None
                )

                results.append({
                    "ticker": p.contract.symbol,
                    "qty": qty,
                    "avg_entry": avg_entry,
                    "current_price": current,
                    "unrealised_pnl": pnl,
                    "unrealised_pnl_pct": pnl_pct,
                })
            return results
        except Exception as exc:
            logger.error("IBKR get_positions failed: %s", exc)
            return []

    def get_order_history(self, limit: int = 20) -> list[dict]:
        if not self._ensure_connected():
            return []

        try:
            # Trades = orders + executions, filled or otherwise
            trades = self._ib.trades()[-limit:]
            results = []
            for t in trades:
                order = t.order
                contract = t.contract
                status = t.orderStatus.status if t.orderStatus else "Unknown"
                filled_qty = (
                    float(t.orderStatus.filled) if t.orderStatus else 0.0
                )
                avg_fill = (
                    float(t.orderStatus.avgFillPrice)
                    if t.orderStatus and t.orderStatus.avgFillPrice
                    else None
                )
                # Take submission/fill timestamps from the trade log if present
                submitted_at = (
                    str(t.log[0].time) if t.log else ""
                )
                filled_at = None
                for entry in t.log:
                    if entry.status == "Filled":
                        filled_at = str(entry.time)
                        break

                results.append({
                    "order_id": str(order.permId or order.orderId),
                    "ticker": contract.symbol,
                    "qty": float(order.totalQuantity) if order.totalQuantity else None,
                    "filled_qty": filled_qty,
                    "side": "buy" if order.action == "BUY" else "sell",
                    "type": order.orderType.lower(),
                    "status": status.lower(),
                    "filled_avg_price": avg_fill,
                    "submitted_at": submitted_at,
                    "filled_at": filled_at,
                })
            return results
        except Exception as exc:
            logger.error("IBKR get_order_history failed: %s", exc)
            return []

    # ── Write operations ─────────────────────────────────────────────────────

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
                "Use the paper IBKR account or set paper_only=True."
            )

        if not self._ensure_connected():
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status="error_disconnected",
                is_paper=self.paper,
            )

        try:
            contract = _Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)

            order = _MarketOrder(
                action="BUY" if side == "buy" else "SELL",
                totalQuantity=qty,
            )
            trade = self._ib.placeOrder(contract, order)

            # Wait briefly for the order to acknowledge / fill (paper fills
            # are usually instant; live can vary)
            self._ib.sleep(1)

            status = trade.orderStatus.status if trade.orderStatus else "Submitted"
            avg_fill = (
                float(trade.orderStatus.avgFillPrice)
                if trade.orderStatus and trade.orderStatus.avgFillPrice
                else None
            )

            logger.info(
                "IBKR order submitted: %s %s x%.2f → %s (paper=%s)",
                side.upper(), ticker, qty, status, self.paper,
            )

            return OrderResult(
                order_id=str(trade.order.permId or trade.order.orderId),
                ticker=ticker,
                qty=qty,
                side=side,
                status=status.lower(),
                is_paper=self.paper,
                fill_price=avg_fill,
            )
        except Exception as exc:
            logger.error("IBKR place_order failed: %s", exc)
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status=f"error: {exc}",
                is_paper=self.paper,
            )

    def close_position(self, ticker: str) -> dict:
        if not self._ensure_connected():
            return {"error": "IBKR not configured"}

        try:
            # Find the open position for this ticker
            target = None
            for p in self._ib.positions():
                if p.contract.symbol == ticker and p.position != 0:
                    target = p
                    break
            if target is None:
                return {"error": f"No open position for {ticker}", "ticker": ticker}

            qty = abs(float(target.position))
            side = "SELL" if target.position > 0 else "BUY"

            contract = _Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            order = _MarketOrder(action=side, totalQuantity=qty)
            trade = self._ib.placeOrder(contract, order)
            self._ib.sleep(1)

            status = trade.orderStatus.status if trade.orderStatus else "Submitted"
            logger.info(
                "IBKR closed position: %s qty=%.2f side=%s status=%s (paper=%s)",
                ticker, qty, side, status, self.paper,
            )
            return {
                "order_id": str(trade.order.permId or trade.order.orderId),
                "ticker": ticker,
                "side": side.lower(),
                "status": status.lower(),
            }
        except Exception as exc:
            logger.error("IBKR close_position(%s) failed: %s", ticker, exc)
            return {"error": str(exc), "ticker": ticker}
