"""
Tests for the broker abstraction layer.

Three layers:
  1. Factory selection — alpaca/ibkr/unknown picks the right class
  2. AlpacaBroker behaviour — disconnected stubs, paper guard
  3. IBKRBroker behaviour — disconnected stubs, paper guard, mocked ib_insync

We never hit a real Alpaca or IBKR API. Tests run on a developer machine
that may not have ib_insync installed at all.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.brokers import BrokerClient, OrderResult, get_broker, init_broker
from backend.brokers.alpaca import AlpacaBroker
from backend.brokers.factory import reset_broker
from backend.brokers.ibkr import IBKRBroker


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_broker()
    yield
    reset_broker()


class TestBrokerFactoryNone:
    """
    BROKER_PROVIDER=none is the explicit "no broker" option used when the
    backend runs somewhere it can't reach a broker (cloud-hosted analysis,
    test environments). Every call site already short-circuits on
    get_broker() returning None; the "none" provider is just the clean way
    to opt in to that state without construction attempts or connection
    timeouts.
    """

    def test_none_provider_returns_none_and_singleton_is_none(self):
        from backend.brokers import get_broker, init_broker

        result = init_broker("none")
        assert result is None
        assert get_broker() is None

    def test_none_provider_ignores_kwargs(self):
        """
        init_broker('none') must accept but ignore provider-specific kwargs,
        so the startup code in main.py can pass them unconditionally without
        branching.
        """
        from backend.brokers import get_broker, init_broker

        init_broker("none", host="127.0.0.1", port=4001, api_key="bogus")
        assert get_broker() is None

    def test_none_provider_is_case_insensitive(self):
        from backend.brokers import get_broker, init_broker

        init_broker("NONE")
        assert get_broker() is None
        init_broker("None")
        assert get_broker() is None


# ── Factory ────────────────────────────────────────────────────────────────

class TestBrokerFactory:
    def test_init_alpaca_returns_alpaca_instance(self):
        broker = init_broker("alpaca", api_key="", secret_key="", paper=True)
        assert isinstance(broker, AlpacaBroker)
        assert get_broker() is broker

    def test_init_ibkr_returns_ibkr_instance(self):
        broker = init_broker("ibkr", host="127.0.0.1", port=7497, client_id=1)
        assert isinstance(broker, IBKRBroker)
        assert get_broker() is broker

    def test_unknown_provider_falls_back_to_alpaca(self):
        broker = init_broker("garbage", api_key="", secret_key="")
        assert isinstance(broker, AlpacaBroker)

    def test_provider_name_case_insensitive(self):
        broker = init_broker("IBKR", host="127.0.0.1", port=7497)
        assert isinstance(broker, IBKRBroker)

    def test_get_broker_returns_none_before_init(self):
        # _clean_singleton resets it
        assert get_broker() is None

    def test_init_replaces_singleton(self):
        a = init_broker("alpaca", api_key="", secret_key="")
        b = init_broker("ibkr", host="127.0.0.1", port=7497)
        assert get_broker() is b
        assert get_broker() is not a


# ── AlpacaBroker ───────────────────────────────────────────────────────────

class TestAlpacaBroker:
    def test_disconnected_when_no_credentials(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        assert b.is_connected() is False
        assert b.get_account()["status"] == "disconnected"
        assert b.get_positions() == []
        assert b.get_order_history() == []

    def test_implements_abstract_interface(self):
        b = AlpacaBroker()
        assert isinstance(b, BrokerClient)

    def test_paper_guard_on_place_order(self):
        """place_order with paper_only=True on a live broker must raise."""
        b = AlpacaBroker(api_key="", secret_key="", paper=False)
        with pytest.raises(ValueError, match="paper-only but broker is in live"):
            b.place_order(ticker="NVDA", qty=1, side="buy", paper_only=True)

    def test_disconnected_place_order_returns_error_result(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        result = b.place_order(ticker="NVDA", qty=1, side="buy", paper_only=True)
        assert isinstance(result, OrderResult)
        assert result.status == "error_disconnected"
        assert result.is_paper is True

    def test_place_order_rejects_limit_without_price(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        with pytest.raises(ValueError, match="limit_price is required"):
            b.place_order(
                ticker="NVDA",
                qty=1,
                side="buy",
                paper_only=True,
                order_type="limit",
            )

    def test_place_order_rejects_unknown_order_type(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        with pytest.raises(ValueError, match="order_type must be"):
            b.place_order(
                ticker="NVDA",
                qty=1,
                side="buy",
                paper_only=True,
                order_type="stop",
            )


# ── IBKRBroker ─────────────────────────────────────────────────────────────

class TestIBKRBroker:
    def test_disconnected_returns_disconnected_status(self):
        """No IB Gateway → is_connected False, get_account returns the
        actionable error message that tells the user how to fix it."""
        b = IBKRBroker(host="127.0.0.1", port=7497, client_id=1, paper=True)
        # _ensure_connected will fail (no Gateway running, ib_insync may not
        # even be installed). is_connected() should return False without raising.
        assert b.is_connected() is False
        account = b.get_account()
        assert account["status"] == "disconnected"
        assert "ib gateway" in account["message"].lower()

    def test_implements_abstract_interface(self):
        b = IBKRBroker()
        assert isinstance(b, BrokerClient)

    def test_paper_guard_on_place_order(self):
        """place_order with paper_only=True on a live broker must raise."""
        b = IBKRBroker(paper=False)
        with pytest.raises(ValueError, match="paper-only but broker is in live"):
            b.place_order(ticker="NVDA", qty=1, side="buy", paper_only=True)

    def test_place_order_rejects_limit_without_price(self):
        b = IBKRBroker(paper=True)
        with pytest.raises(ValueError, match="limit_price is required"):
            b.place_order(
                ticker="NVDA",
                qty=1,
                side="buy",
                paper_only=True,
                order_type="limit",
            )

    def test_get_positions_no_gateway(self):
        b = IBKRBroker()
        assert b.get_positions() == []

    def test_get_order_history_no_gateway(self):
        b = IBKRBroker()
        assert b.get_order_history() == []

    def test_close_position_no_gateway(self):
        b = IBKRBroker()
        result = b.close_position("NVDA")
        assert "error" in result

    def test_place_order_with_mocked_ib_insync(self):
        """
        Build an IBKRBroker, inject a mocked ib_insync IB instance, and verify
        the place_order path calls into it with the expected shape.

        Note on threading: ib_insync's thread-affinity issues forced us to
        run every operation on a dedicated background thread that owns the
        event loop. The broker methods submit coroutines via
        run_coroutine_threadsafe. For tests we can use the real broker loop
        (it's a module-level singleton) and inject an AsyncMock-shaped IB
        instance that supports `await`ing its async methods.
        """
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)

        # Build a fake trade object that looks like ib_insync's Trade
        fake_order = SimpleNamespace(permId=12345, orderId=1)
        fake_status = SimpleNamespace(status="Filled", avgFillPrice=450.12)
        fake_trade = SimpleNamespace(
            order=fake_order,
            orderStatus=fake_status,
            log=[],
        )

        # The IB mock needs async methods (qualifyContractsAsync) that can
        # actually be awaited, plus sync methods (placeOrder, isConnected)
        # that return values directly.
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.qualifyContractsAsync = AsyncMock(return_value=[])
        fake_ib.placeOrder = MagicMock(return_value=fake_trade)

        b._ib = fake_ib  # bypass _ensure_connected by pre-injecting

        # Patch the module-level _Stock / _MarketOrder constants so the
        # broker's internal calls resolve to something harmless
        with patch("backend.brokers.ibkr._Stock", return_value=SimpleNamespace(symbol="NVDA")), \
             patch("backend.brokers.ibkr._MarketOrder", return_value=fake_order):
            result = b.place_order(
                ticker="NVDA", qty=10, side="buy", paper_only=True
            )

        assert isinstance(result, OrderResult)
        assert result.order_id == "12345"
        assert result.ticker == "NVDA"
        assert result.qty == 10
        assert result.side == "buy"
        assert result.status == "filled"
        assert result.fill_price == 450.12
        assert result.is_paper is True
        # Verify the IBKR client was actually called
        fake_ib.placeOrder.assert_called_once()
        fake_ib.qualifyContractsAsync.assert_awaited_once()

    def test_place_order_limit_path_calls_limit_order(self):
        """
        Non-bracket limit orders MUST construct _LimitOrder(lmtPrice=...),
        not _MarketOrder. Regression guard for the silent-market-downgrade
        bug the HANDOFF flagged as pre-live blocker.
        """
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)

        fake_order = SimpleNamespace(permId=99999, orderId=2)
        fake_status = SimpleNamespace(status="Submitted", avgFillPrice=None)
        fake_trade = SimpleNamespace(order=fake_order, orderStatus=fake_status, log=[])

        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.qualifyContractsAsync = AsyncMock(return_value=[])
        fake_ib.placeOrder = MagicMock(return_value=fake_trade)
        b._ib = fake_ib

        limit_order_mock = MagicMock(return_value=fake_order)
        market_order_mock = MagicMock(return_value=fake_order)

        with patch("backend.brokers.ibkr._Stock", return_value=SimpleNamespace(symbol="NVDA")), \
             patch("backend.brokers.ibkr._LimitOrder", limit_order_mock), \
             patch("backend.brokers.ibkr._MarketOrder", market_order_mock):
            result = b.place_order(
                ticker="NVDA",
                qty=5,
                side="buy",
                paper_only=True,
                order_type="limit",
                limit_price=123.45,
            )

        assert isinstance(result, OrderResult)
        assert result.status == "submitted"
        limit_order_mock.assert_called_once_with(
            action="BUY", totalQuantity=5, lmtPrice=123.45
        )
        market_order_mock.assert_not_called()


# ── Pending orders + cancel ──────────────────────────────────────────────────

class TestPendingOrdersAndCancel:
    """
    Broker ABC methods get_pending_orders() and cancel_order(). Covers
    disconnected behaviour (never raise, return empty/error dict), plus the
    IBKR cancel happy path via a mocked IB instance.
    """

    def test_alpaca_disconnected_get_pending_returns_empty(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        assert b.get_pending_orders() == []

    def test_alpaca_disconnected_cancel_returns_error(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        result = b.cancel_order("abc-123")
        assert "error" in result
        assert result["order_id"] == "abc-123"

    def test_ibkr_disconnected_get_pending_returns_empty(self):
        b = IBKRBroker()
        assert b.get_pending_orders() == []

    def test_ibkr_disconnected_cancel_returns_error(self):
        b = IBKRBroker()
        result = b.cancel_order("123456")
        assert "error" in result
        assert result["order_id"] == "123456"

    def test_ibkr_cancel_not_found_returns_clear_error(self):
        """
        Cancelling an orderId that isn't among openTrades must return a
        specific not-found error so the endpoint can map it to HTTP 404
        (meaning "already filled/cancelled" — the safer interpretation).
        """
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = []  # no working orders
        fake_ib.cancelOrder = MagicMock()
        b._ib = fake_ib

        result = b.cancel_order("99999")
        assert "error" in result
        assert "not found" in result["error"].lower()
        fake_ib.cancelOrder.assert_not_called()

    def test_ibkr_cancel_calls_cancelOrder_on_matched_trade(self):
        """Happy path: found a matching trade by permId, called cancelOrder."""
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)

        fake_order = SimpleNamespace(permId=1046335652, orderId=9)
        fake_status = SimpleNamespace(status="Cancelled")
        fake_trade = SimpleNamespace(order=fake_order, orderStatus=fake_status, log=[])

        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = [fake_trade]
        fake_ib.cancelOrder = MagicMock()
        b._ib = fake_ib

        result = b.cancel_order("1046335652")
        assert result["status"] == "cancelled"
        assert result["order_id"] == "1046335652"
        fake_ib.cancelOrder.assert_called_once_with(fake_order)

    def test_ibkr_cancel_falls_back_to_orderId_when_permId_missing(self):
        """
        Pre-session-persistence flows may only have orderId (permId set
        after the first TWS ack). Must still match on orderId.
        """
        b = IBKRBroker(paper=True)

        fake_order = SimpleNamespace(permId=None, orderId=42)
        fake_status = SimpleNamespace(status="Cancelled")
        fake_trade = SimpleNamespace(order=fake_order, orderStatus=fake_status, log=[])

        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = [fake_trade]
        fake_ib.cancelOrder = MagicMock()
        b._ib = fake_ib

        result = b.cancel_order("42")
        assert result["status"] == "cancelled"
        fake_ib.cancelOrder.assert_called_once_with(fake_order)


# ── Scale-out bracket plumbing (target_qty parameter) ─────────────────────────

class TestScaleOutBracketSignature:
    """
    Commit 1 of the breakeven-stop feature wires target_qty through the
    ABC, both broker impls, and the endpoint. The actual scale-out order
    construction lands in the next commit; for now, both brokers must
    explicitly raise NotImplementedError when target_qty < qty so callers
    don't silently fall back to an all-out bracket with the wrong qty.
    """

    def test_alpaca_raises_notimplemented_on_scale_out(self):
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        with pytest.raises(NotImplementedError, match="[Ss]cale-out"):
            b.place_bracket_order(
                ticker="SPY",
                qty=2,
                side="buy",
                limit_price=540.0,
                stop_loss_price=534.0,
                take_profit_price=552.0,
                paper_only=True,
                target_qty=1,
            )

    def test_alpaca_accepts_target_qty_equal_to_qty(self):
        """
        target_qty == qty is not scale-out — it's the classic all-out
        bracket expressed explicitly. Must not raise. Since the broker
        isn't connected we get error_disconnected; the point is we don't
        hit the NotImplementedError path.
        """
        b = AlpacaBroker(api_key="", secret_key="", paper=True)
        result = b.place_bracket_order(
            ticker="SPY",
            qty=2,
            side="buy",
            limit_price=540.0,
            stop_loss_price=534.0,
            take_profit_price=552.0,
            paper_only=True,
            target_qty=2,
        )
        assert result.status == "error_disconnected"

    def test_ibkr_scale_out_places_four_orders(self):
        """
        Scale-out bracket is constructed as 4 orders: parent + OCA'd (TP +
        SL_A on the T1 portion) + independent SL_B on the runner. All
        placeOrder calls happen on the broker loop; the test mocks the
        IB client so we verify order shapes without hitting TWS.
        """
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)

        fake_order = SimpleNamespace(permId=888, orderId=42)
        fake_status = SimpleNamespace(status="Submitted", avgFillPrice=None)
        fake_parent_trade = SimpleNamespace(
            order=fake_order, orderStatus=fake_status, log=[],
        )

        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.qualifyContractsAsync = AsyncMock(return_value=[])
        fake_ib.placeOrder = MagicMock(return_value=fake_parent_trade)
        b._ib = fake_ib

        # We need the parent Order to expose an orderId attribute that
        # ib_insync normally assigns on placeOrder. _LimitOrder is a real
        # class with attribute access, so we just let the ctor create one
        # and rely on its default orderId=0 (unique enough for a mocked test).
        with patch("backend.brokers.ibkr._Stock", return_value=SimpleNamespace(symbol="SPY")):
            result = b.place_bracket_order(
                ticker="SPY",
                qty=4,
                side="buy",
                limit_price=540.0,
                stop_loss_price=534.0,
                take_profit_price=552.0,
                paper_only=True,
                target_qty=1,
            )

        assert isinstance(result, OrderResult)
        # Parent + TP + SL_A + SL_B = 4 placeOrder calls
        assert fake_ib.placeOrder.call_count == 4

        # Extract the Order object from each placeOrder call
        placed_orders = [call.args[1] for call in fake_ib.placeOrder.call_args_list]
        actions = [o.action for o in placed_orders]
        quantities = [o.totalQuantity for o in placed_orders]

        assert actions == ["BUY", "SELL", "SELL", "SELL"]
        # Parent buys 4, TP sells 1, SL_A sells 1, SL_B sells 3 (runner)
        assert quantities == [4, 1, 1, 3]

        # TP + SL_A share an OCA group so one cancels the other on fill.
        # SL_B has no OCA so it survives independently.
        tp_a = placed_orders[1]
        sl_a = placed_orders[2]
        sl_b = placed_orders[3]
        assert tp_a.ocaGroup and sl_a.ocaGroup and tp_a.ocaGroup == sl_a.ocaGroup
        assert not getattr(sl_b, "ocaGroup", None)

        # Only the last order has transmit=True so TWS submits the whole
        # group atomically. The first three must be transmit=False.
        transmits = [getattr(o, "transmit", True) for o in placed_orders]
        assert transmits == [False, False, False, True]

    def test_ibkr_scale_out_rejects_fractional_qty(self):
        b = IBKRBroker(paper=True)
        with pytest.raises(ValueError, match="integer"):
            b.place_bracket_order(
                ticker="SPY",
                qty=2.5,
                side="buy",
                limit_price=540.0,
                stop_loss_price=534.0,
                take_profit_price=552.0,
                paper_only=True,
                target_qty=1,
            )

    def test_ibkr_scale_out_rejects_qty_too_small(self):
        """qty=2, target_qty=1 is the minimum valid scale-out. qty=1 can't split."""
        b = IBKRBroker(paper=True)
        with pytest.raises(ValueError, match="at least 1 share"):
            b.place_bracket_order(
                ticker="SPY",
                qty=1,
                side="buy",
                limit_price=540.0,
                stop_loss_price=534.0,
                take_profit_price=552.0,
                paper_only=True,
                target_qty=0,
            )
