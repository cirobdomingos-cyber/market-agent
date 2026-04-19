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
