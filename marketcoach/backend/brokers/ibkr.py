"""
IBKRBroker — BrokerClient implementation for Interactive Brokers via ib_insync.

Why IBKR:
  Alpaca stopped accepting Brazilian residents for live accounts. IBKR is
  the only serious retail broker that accepts Brazilian residents AND has
  a real API. This implementation lets MarketCoach swap brokers at startup
  with a single settings flag — no agent or endpoint code changes needed.

How IBKR is different from Alpaca:
  - IBKR talks via a TCP socket to a local IB Gateway (or TWS) process,
    NOT over HTTPS. You must run Gateway on the same machine, logged in.
  - The official ibapi library is callback-based. We use ib_insync, which
    wraps it in a clean sync/async API.
  - Paper vs live is decided by which Gateway login you use. Same code,
    different login. Gateway 10+ ports: 4002 = paper, 4001 = live.
    (Older IB Gateway / TWS uses 7497 paper / 7496 live.)

Threading model — the tricky part
---------------------------------
ib_insync is fundamentally single-threaded. The IB() client maintains
async state (reader/writer tasks, callback queues, nest_asyncio glue) that
is bound to the event loop that was running when `ib.connect()` was called.
Calling any ib.* method from a DIFFERENT thread sends the request on the
socket but the response callback is scheduled on the original thread's
loop, so the caller waits forever for a result that never arrives on the
current thread.

FastAPI's sync endpoints run in an anyio threadpool — every request may
land in a different worker thread. The first /portfolio call connected
on thread A and worked. The next call landed on thread B, tried to use
the same IB client, and hung inside placeOrder's sleep(1) waiting for a
fill callback that was being delivered to A's loop.

Fix: run ALL ib_insync operations on a single dedicated background thread
that owns the event loop. FastAPI worker threads submit coroutines via
asyncio.run_coroutine_threadsafe and block on the resulting Future. This
is the canonical pattern for integrating blocking sync code with an async
library that assumes single-threaded ownership.

Cost: all IB operations serialize through one thread. For a personal
trading account with a handful of operations per minute that's a non-issue.
For a high-frequency system it would be a bottleneck, but we aren't one.
"""

import asyncio
import concurrent.futures
import logging
import threading
from typing import Optional

from backend.brokers.base import BrokerClient, OrderResult

logger = logging.getLogger(__name__)


# ── ib_insync import at module load time ────────────────────────────────────
#
# The eventkit library (ib_insync's dependency) calls asyncio.get_event_loop()
# at import time to cache a "main" loop. Importing lazily from a worker
# thread raises "no current event loop". We import here so the init runs on
# the main thread during FastAPI startup, where a loop exists.

try:
    from ib_insync import IB as _IB
    from ib_insync import Stock as _Stock
    from ib_insync import MarketOrder as _MarketOrder
    from ib_insync import LimitOrder as _LimitOrder
    from ib_insync import StopOrder as _StopOrder
    _IBKR_AVAILABLE = True
except ImportError:  # pragma: no cover — graceful degradation
    _IB = None
    _Stock = None
    _MarketOrder = None
    _LimitOrder = None
    _StopOrder = None
    _IBKR_AVAILABLE = False
    logger.debug("ib_insync not installed — IBKR broker will refuse connections")


# ── Dedicated broker thread + event loop ────────────────────────────────────
#
# Module-level singletons. The first call to `_get_broker_loop()` starts a
# daemon thread that creates its own event loop and runs it forever. All
# subsequent ib_insync operations are submitted to this loop via
# `asyncio.run_coroutine_threadsafe` from whatever FastAPI worker thread
# happens to be handling the request.
#
# The loop is created eagerly (so ib_insync's nest_asyncio tricks have a
# stable environment) but the IBKRBroker.connect happens later when the
# first /portfolio call fires. That keeps startup fast and means a bad
# IBKR config doesn't crash the backend at boot.

_broker_thread: Optional[threading.Thread] = None
_broker_loop: Optional[asyncio.AbstractEventLoop] = None
_broker_thread_lock = threading.Lock()


def _get_broker_loop() -> asyncio.AbstractEventLoop:
    """Return the shared broker event loop, starting the thread on first call."""
    global _broker_thread, _broker_loop
    with _broker_thread_lock:
        if _broker_loop is not None and _broker_thread is not None and _broker_thread.is_alive():
            return _broker_loop

        loop = asyncio.new_event_loop()

        def _run_loop():
            # This thread owns the loop forever. Every ib_insync op is
            # scheduled on it via run_coroutine_threadsafe from elsewhere.
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                # Graceful shutdown — cancel pending tasks, then close.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
                loop.close()

        thread = threading.Thread(
            target=_run_loop,
            daemon=True,
            name="ibkr-broker-loop",
        )
        thread.start()
        _broker_thread = thread
        _broker_loop = loop
        logger.info("IBKR broker loop started on dedicated thread")
        return loop


def _run_on_broker_loop(coro, timeout: float = 30.0):
    """
    Submit a coroutine to the broker loop's thread and block until done.

    Args:
        coro: an `async def` coroutine that uses ib_insync's async variants
        timeout: seconds to wait before giving up

    Raises:
        concurrent.futures.TimeoutError if the call takes longer than timeout
        Any exception raised inside the coroutine is re-raised here
    """
    loop = _get_broker_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


# ── Broker class ────────────────────────────────────────────────────────────

class IBKRBroker(BrokerClient):
    """
    Interactive Brokers client via ib_insync, running all ops on a dedicated
    background thread to avoid the thread-affinity issues that would otherwise
    break every call after the first.
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
        self._ib = None  # the shared IB() instance, created on first connect
        self._connect_lock = threading.Lock()

    # ── Connection lifecycle ────────────────────────────────────────────────

    def _ensure_connected(self) -> bool:
        """
        Connect to IB Gateway on first use. Idempotent: subsequent calls just
        verify the existing connection is still live. All the ib_insync work
        happens on the broker loop thread.
        """
        if not _IBKR_AVAILABLE:
            logger.warning(
                "ib_insync not installed — install with `pip install ib_insync` "
                "and start IB Gateway to enable IBKR broker"
            )
            return False

        with self._connect_lock:
            if self._ib is not None and self._ib.isConnected():
                return True

            async def _do_connect():
                ib = _IB()
                await ib.connectAsync(
                    self.host,
                    self.port,
                    clientId=self.client_id,
                    timeout=10,
                )
                return ib

            try:
                self._ib = _run_on_broker_loop(_do_connect(), timeout=15.0)
                logger.info(
                    "IBKR broker connected to %s:%d clientId=%d (paper=%s)",
                    self.host, self.port, self.client_id, self.paper,
                )
                return True
            except Exception as exc:
                logger.warning("IBKR connection failed: %s", exc)
                return False

    def is_connected(self) -> bool:
        # Cheap check — reads a boolean attribute on the IB instance.
        # Doesn't need to cross the loop boundary.
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

        async def _do():
            # reqAccountSummaryAsync returns AccountValue rows keyed by tag
            rows = await self._ib.accountSummaryAsync()
            return rows

        try:
            summary = _run_on_broker_loop(_do(), timeout=10.0)
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

        async def _do():
            positions = await self._ib.reqPositionsAsync()
            return list(positions)

        try:
            positions = _run_on_broker_loop(_do(), timeout=10.0)
            # Enrich each position with a current price from yfinance.
            # IBKR's built-in market data would require a real-time
            # subscription (~$1.50/mo) AND a slow cross-loop snapshot call;
            # yfinance is free, returns real-time-ish last trade, and we
            # already depend on it via the market_data tool. Single lookup
            # per position — cheap for a handful, still fine for dozens.
            from backend.tools.market_data import execute_market_data
            results = []
            for p in positions:
                qty = float(p.position)
                avg_entry = float(p.avgCost)
                ticker = p.contract.symbol

                current_price = None
                if qty != 0:  # don't bother for tombstone rows
                    try:
                        quote = execute_market_data(action="quote", ticker=ticker)
                        if isinstance(quote, dict) and quote.get("price"):
                            current_price = float(quote["price"])
                    except Exception as exc:
                        logger.debug(
                            "Current price lookup failed for %s: %s",
                            ticker, exc,
                        )

                # Derive P&L only if we have both prices
                if current_price is not None and avg_entry > 0:
                    unrealised_pnl = (current_price - avg_entry) * qty
                    unrealised_pnl_pct = (
                        ((current_price - avg_entry) / avg_entry) * 100
                    )
                else:
                    unrealised_pnl = None
                    unrealised_pnl_pct = None

                results.append({
                    "ticker": ticker,
                    "qty": qty,
                    "avg_entry": avg_entry,
                    "current_price": current_price,
                    "unrealised_pnl": unrealised_pnl,
                    "unrealised_pnl_pct": unrealised_pnl_pct,
                })
            return results
        except Exception as exc:
            logger.error("IBKR get_positions failed: %s", exc)
            return []

    def get_order_history(self, limit: int = 20) -> list[dict]:
        if not self._ensure_connected():
            return []

        async def _do():
            # trades() is a synchronous list accessor on IB — cheap.
            # Wrap in async so it runs on the broker loop.
            return list(self._ib.trades())[-limit:]

        try:
            trades = _run_on_broker_loop(_do(), timeout=5.0)
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
                submitted_at = str(t.log[0].time) if t.log else ""
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
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        if order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got {order_type!r}")
        if order_type == "limit" and limit_price is None:
            raise ValueError("limit_price is required when order_type='limit'")
        if paper_only and not self.paper:
            raise ValueError(
                "Caller requested paper-only but broker is in live mode. "
                "Pass paper_only=False to confirm live trading intent."
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

        async def _do():
            contract = _Stock(ticker, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            action = "BUY" if side == "buy" else "SELL"
            if order_type == "limit":
                order = _LimitOrder(
                    action=action,
                    totalQuantity=qty,
                    lmtPrice=limit_price,
                )
            else:
                order = _MarketOrder(
                    action=action,
                    totalQuantity=qty,
                )
            trade = self._ib.placeOrder(contract, order)
            # Wait briefly for the order status to populate — paper fills
            # usually arrive within <1s. Use asyncio.sleep on the broker loop
            # instead of ib.sleep() which is a sync wrapper that confuses
            # nest_asyncio when called via run_coroutine_threadsafe.
            await asyncio.sleep(2.0)
            return trade

        try:
            trade = _run_on_broker_loop(_do(), timeout=15.0)

            status = trade.orderStatus.status if trade.orderStatus else "Submitted"
            avg_fill = (
                float(trade.orderStatus.avgFillPrice)
                if trade.orderStatus and trade.orderStatus.avgFillPrice
                else None
            )

            logger.info(
                "IBKR order submitted: %s %s x%.2f → %s (paper=%s, fill=%s)",
                side.upper(), ticker, qty, status, self.paper, avg_fill,
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

    def place_bracket_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        paper_only: bool = True,
        target_qty: Optional[float] = None,
    ) -> OrderResult:
        """
        IBKR brackets use ib_insync's helper which returns [parent, takeProfit,
        stopLoss]. The three orders share a parentId so TWS binds them as an
        OCO group automatically — when one exit fills, the other cancels.

        We submit all three with placeOrder() but only return the PARENT's
        OrderResult. The exit legs live at the broker and don't appear in
        our executed_orders table until they fill (at which point the
        position poll + journal flow handles the close).

        target_qty is accepted for interface parity with the ABC but the
        scale-out construction lands in a follow-up commit. For now,
        scale-out requests raise NotImplementedError so callers don't
        silently fall back to the wrong structure.
        """
        if side != "buy":
            raise ValueError("Bracket orders only support BUY (long entries) in v1")
        is_scale_out = target_qty is not None and target_qty < qty
        if is_scale_out:
            if target_qty != int(target_qty) or qty != int(qty):
                raise ValueError(
                    "Scale-out brackets require integer qty and target_qty"
                )
            if target_qty < 1 or qty - target_qty < 1:
                raise ValueError(
                    "Scale-out requires at least 1 share on both the T1 leg "
                    "and the runner leg (so qty >= 2 and 1 <= target_qty < qty)"
                )
        if paper_only and not self.paper:
            raise ValueError(
                "Caller requested paper-only but broker is in live mode. "
                "Pass paper_only=False to confirm live trading intent."
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

        async def _do():
            contract = _Stock(ticker, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)

            if not is_scale_out:
                # Classic all-out bracket: ib_insync's helper wires parentId
                # + tif=GTC for us. Returns [parent, takeProfit, stopLoss].
                bracket = self._ib.bracketOrder(
                    action="BUY",
                    quantity=qty,
                    limitPrice=limit_price,
                    takeProfitPrice=take_profit_price,
                    stopLossPrice=stop_loss_price,
                )
                trades = [self._ib.placeOrder(contract, o) for o in bracket]
                await asyncio.sleep(2.0)
                return trades

            # Scale-out bracket. The helper doesn't support this shape, so
            # we build four orders by hand:
            #
            #   parent BUY N        — entry (limit, tif=GTC, transmit=False)
            #   TP SELL target_qty  — take-profit on the T1 portion
            #                         (OCA group A, cancels SL_A on fill)
            #   SL_A SELL target_qty— stop on the T1 portion
            #                         (OCA group A, cancels TP on fill)
            #   SL_B SELL remainder — runner's stop, no OCA group
            #                         (becomes the breakeven-move target)
            #
            # When TP fills, OCA cancels SL_A. Runner shares are still
            # covered by SL_B at the same initial stop. The state-machine
            # in the position poll (commit 3) detects the TP fill via the
            # existing Position delta logic and modifies SL_B.lmtPrice /
            # auxPrice to the entry price → breakeven stop active.
            runner_qty = qty - target_qty
            # Prevents stop+target from colliding with any existing order
            # group on the account. Keep unique per-ticker-per-session.
            import time as _time
            oca_group = f"mc_scaleout_{ticker}_{int(_time.time())}"

            parent = _LimitOrder(
                action="BUY",
                totalQuantity=qty,
                lmtPrice=limit_price,
                tif="GTC",
                transmit=False,  # children need parentId first
            )
            parent_trade = self._ib.placeOrder(contract, parent)
            parent_id = parent.orderId  # ib_insync assigns after placeOrder

            tp_a = _LimitOrder(
                action="SELL",
                totalQuantity=target_qty,
                lmtPrice=take_profit_price,
                parentId=parent_id,
                ocaGroup=oca_group,
                ocaType=1,  # cancel-all-remaining-with-block
                tif="GTC",
                transmit=False,
            )
            sl_a = _StopOrder(
                action="SELL",
                totalQuantity=target_qty,
                stopPrice=stop_loss_price,
                parentId=parent_id,
                ocaGroup=oca_group,
                ocaType=1,
                tif="GTC",
                transmit=False,
            )
            sl_b = _StopOrder(
                action="SELL",
                totalQuantity=runner_qty,
                stopPrice=stop_loss_price,
                parentId=parent_id,
                tif="GTC",
                transmit=True,  # last order triggers group submission
            )

            self._ib.placeOrder(contract, tp_a)
            self._ib.placeOrder(contract, sl_a)
            self._ib.placeOrder(contract, sl_b)

            await asyncio.sleep(2.0)
            return [parent_trade]

        try:
            trades = _run_on_broker_loop(_do(), timeout=15.0)
            parent = trades[0]

            status = parent.orderStatus.status if parent.orderStatus else "Submitted"
            avg_fill = (
                float(parent.orderStatus.avgFillPrice)
                if parent.orderStatus and parent.orderStatus.avgFillPrice
                else None
            )

            scale_tag = (
                f" [SCALE-OUT: T1={target_qty}, runner={qty - target_qty}]"
                if is_scale_out else ""
            )
            logger.info(
                "IBKR bracket submitted: BUY %s x%.2f @ $%.2f "
                "(stop $%.2f, target $%.2f)%s → %s (paper=%s, fill=%s)",
                ticker, qty, limit_price, stop_loss_price, take_profit_price,
                scale_tag, status, self.paper, avg_fill,
            )

            return OrderResult(
                order_id=str(parent.order.permId or parent.order.orderId),
                ticker=ticker,
                qty=qty,
                side=side,
                status=status.lower(),
                is_paper=self.paper,
                fill_price=avg_fill,
            )
        except ValueError:
            raise
        except Exception as exc:
            logger.error("IBKR place_bracket_order failed: %s", exc)
            return OrderResult(
                order_id="error",
                ticker=ticker,
                qty=qty,
                side=side,
                status=f"error: {exc}",
                is_paper=self.paper,
            )

    def get_pending_orders(self) -> list[dict]:
        """
        Return IBKR working orders — anything not yet filled or cancelled.
        Uses openTrades() which returns Trade objects with their live
        OrderStatus attached. For brackets, each leg (parent + stop +
        take-profit) appears as its own row; parent_id is set on legs so
        the frontend can group them under the parent.
        """
        if not self._ensure_connected():
            return []

        async def _do():
            return list(self._ib.openTrades())

        try:
            trades = _run_on_broker_loop(_do(), timeout=5.0)
            results = []
            for t in trades:
                order = t.order
                contract = t.contract
                status = t.orderStatus.status if t.orderStatus else "Unknown"
                filled_qty = (
                    float(t.orderStatus.filled) if t.orderStatus else 0.0
                )
                submitted_at = str(t.log[0].time) if t.log else ""
                # Non-zero parentId means this is a bracket leg. Use permId
                # if set (stable) else fall back to orderId (session-local).
                parent_id = None
                if order.parentId and order.parentId != 0:
                    parent_id = str(order.parentId)
                # Normalise orderType to match Alpaca's lowercase ('limit',
                # 'market', 'stop') so the frontend sees one shape.
                ot = (order.orderType or "").upper()
                if ot == "LMT":
                    order_type = "limit"
                elif ot == "MKT":
                    order_type = "market"
                elif ot == "STP":
                    order_type = "stop"
                else:
                    order_type = ot.lower()
                # Bracket detection: IBKR doesn't flag order_class natively,
                # but any order with a parentId is a leg, and any order with
                # ocaGroup set is part of an OCO group.
                order_class = (
                    "bracket"
                    if (parent_id or getattr(order, "ocaGroup", None))
                    else "simple"
                )

                results.append({
                    "order_id": str(order.permId or order.orderId),
                    "ticker": contract.symbol,
                    "qty": float(order.totalQuantity) if order.totalQuantity else None,
                    "filled_qty": filled_qty,
                    "side": "buy" if order.action == "BUY" else "sell",
                    "order_type": order_type,
                    "order_class": order_class,
                    "status": status.lower(),
                    "limit_price": (
                        float(order.lmtPrice)
                        if getattr(order, "lmtPrice", 0)
                        else None
                    ),
                    "stop_price": (
                        float(order.auxPrice)
                        if getattr(order, "auxPrice", 0)
                        else None
                    ),
                    "submitted_at": submitted_at,
                    "parent_id": parent_id,
                })
            return results
        except Exception as exc:
            logger.error("IBKR get_pending_orders failed: %s", exc)
            return []

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel a working order by its permId (or orderId as fallback).
        ib_insync's cancelOrder takes an Order object, not an ID, so we
        first resolve the ID to a Trade via openTrades() and then cancel
        its .order. For brackets, cancelling the parent auto-cancels the
        OCO legs at TWS — we don't iterate legs ourselves.
        """
        if not self._ensure_connected():
            return {"error": "IBKR not configured", "order_id": order_id}

        async def _do():
            trades = list(self._ib.openTrades())
            target = None
            for t in trades:
                pid = str(t.order.permId) if t.order.permId else None
                oid = str(t.order.orderId) if t.order.orderId else None
                if order_id in (pid, oid):
                    target = t
                    break
            if target is None:
                return {"_not_found": True}
            self._ib.cancelOrder(target.order)
            # Brief wait to let TWS emit the Cancelled status before we log
            await asyncio.sleep(1.0)
            return {"_status": target.orderStatus.status if target.orderStatus else "Unknown"}

        try:
            result = _run_on_broker_loop(_do(), timeout=10.0)
            if result.get("_not_found"):
                return {
                    "error": f"Order {order_id} not found among open trades. "
                             "It may have already filled or been cancelled.",
                    "order_id": order_id,
                }
            logger.info(
                "IBKR cancel submitted for order %s (status=%s, paper=%s)",
                order_id, result.get("_status"), self.paper,
            )
            return {"status": "cancelled", "order_id": order_id}
        except Exception as exc:
            logger.error("IBKR cancel_order failed for %s: %s", order_id, exc)
            return {"error": str(exc), "order_id": order_id}

    def find_open_stop_for_ticker_qty(
        self,
        ticker: str,
        qty: float,
    ) -> Optional[dict]:
        """
        Locate an open SELL STOP order for the given ticker + qty. Returns
        a small dict {order_id, stop_price, orderId} or None if no match.

        Used by the scale-out bracket state machine to find the runner's
        stop after the T1 leg fills — the runner is the unique open stop
        order sized exactly (qty - target_qty). IBKR-specific for now;
        Alpaca would use its open-orders API with the same filter.
        """
        if not self._ensure_connected():
            return None

        async def _do():
            trades = list(self._ib.openTrades())
            for t in trades:
                order = t.order
                if getattr(order, "orderType", "").upper() != "STP":
                    continue
                if getattr(order, "action", "") != "SELL":
                    continue
                if float(order.totalQuantity or 0) != float(qty):
                    continue
                if t.contract.symbol.upper() != ticker.upper():
                    continue
                return {
                    "order_id": str(order.permId or order.orderId),
                    "stop_price": float(order.auxPrice or 0.0),
                    "orderId": order.orderId,
                }
            return None

        try:
            return _run_on_broker_loop(_do(), timeout=5.0)
        except Exception as exc:
            logger.warning(
                "IBKR find_open_stop_for_ticker_qty(%s, %s) failed: %s",
                ticker, qty, exc,
            )
            return None

    def modify_stop_price(self, order_id: str, new_stop_price: float) -> dict:
        """
        Move the trigger price of a working stop order without cancelling
        it. IBKR supports true order modification: re-call placeOrder with
        the same orderId and a changed auxPrice. No cancel/replace window
        where the position is unprotected — the broker swaps the trigger
        level in place.
        """
        if not self._ensure_connected():
            return {"error": "IBKR not configured", "order_id": order_id}

        async def _do():
            trades = list(self._ib.openTrades())
            target = None
            for t in trades:
                pid = str(t.order.permId) if t.order.permId else None
                oid = str(t.order.orderId) if t.order.orderId else None
                if order_id in (pid, oid):
                    target = t
                    break
            if target is None:
                return {"_not_found": True}
            target.order.auxPrice = new_stop_price
            self._ib.placeOrder(target.contract, target.order)
            await asyncio.sleep(1.0)
            return {"_ok": True}

        try:
            result = _run_on_broker_loop(_do(), timeout=10.0)
            if result.get("_not_found"):
                return {
                    "error": f"Order {order_id} not found among open trades",
                    "order_id": order_id,
                }
            logger.info(
                "IBKR modified stop: order %s → new stop $%.2f",
                order_id, new_stop_price,
            )
            return {
                "status": "modified",
                "order_id": order_id,
                "new_stop_price": new_stop_price,
            }
        except Exception as exc:
            logger.error("IBKR modify_stop_price failed for %s: %s", order_id, exc)
            return {"error": str(exc), "order_id": order_id}

    def close_position(self, ticker: str) -> dict:
        if not self._ensure_connected():
            return {"error": "IBKR not configured"}

        async def _do():
            positions = await self._ib.reqPositionsAsync()
            target = None
            for p in positions:
                if p.contract.symbol == ticker and p.position != 0:
                    target = p
                    break
            if target is None:
                return None  # signals "no position" to the sync caller

            qty = abs(float(target.position))
            side = "SELL" if target.position > 0 else "BUY"
            contract = _Stock(ticker, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            order = _MarketOrder(action=side, totalQuantity=qty)
            trade = self._ib.placeOrder(contract, order)
            await asyncio.sleep(2.0)
            return trade, side

        try:
            result = _run_on_broker_loop(_do(), timeout=15.0)
            if result is None:
                return {"error": f"No open position for {ticker}", "ticker": ticker}
            trade, side = result

            status = trade.orderStatus.status if trade.orderStatus else "Submitted"
            logger.info(
                "IBKR closed position: %s side=%s status=%s (paper=%s)",
                ticker, side, status, self.paper,
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
