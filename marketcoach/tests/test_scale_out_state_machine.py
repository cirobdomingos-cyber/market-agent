"""
Tests for the scale-out bracket state machine.

The state machine lives on Orchestrator._advance_scale_out_state_machine
and runs inside the position poll. It walks 'fresh' scale-out brackets,
detects T1 fills via position-qty delta, modifies the runner's stop to
breakeven at the broker, and flips bracket_state to 't1_hit'. Also
covers the IBKR find_open_stop_for_ticker_qty + modify_stop_price
building blocks.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.brokers.ibkr import IBKRBroker
from backend.db.models import Base, ExecutedOrder


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine)


@pytest.fixture(autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def db():
    s = _TestSession()
    yield s
    s.close()


def _make_orch(db):
    from backend.agents.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.db = db
    orch.client = MagicMock()
    return orch


def _seed_scale_out(
    db,
    ticker="SPY",
    qty=4,
    target_qty=1,
    entry=540.0,
    stop=534.0,
    target=552.0,
    state="fresh",
):
    row = ExecutedOrder(
        alpaca_order_id="permId-parent",
        ticker=ticker,
        side="buy",
        qty=float(qty),
        order_type="limit",
        order_class="scale_out",
        limit_price=entry,
        stop_loss_price=stop,
        take_profit_price=target,
        target_qty=float(target_qty),
        bracket_state=state,
        status="accepted",
        is_paper=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestStateMachineTransitions:
    """Core fresh → t1_hit → closed flow under mocked broker state."""

    def test_fresh_with_full_position_stays_fresh(self, db):
        """T1 hasn't filled yet — position is still at qty. No transition."""
        row = _seed_scale_out(db, qty=4, target_qty=1)

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = [{"ticker": "SPY", "qty": 4.0}]
        broker.find_open_stop_for_ticker_qty = MagicMock()
        broker.modify_stop_price = MagicMock()

        orch = _make_orch(db)
        with patch("backend.notifications.notify") as mock_notify:
            transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 0
        db.refresh(row)
        assert row.bracket_state == "fresh"
        broker.modify_stop_price.assert_not_called()
        mock_notify.assert_not_called()

    def test_t1_fill_transitions_to_t1_hit_and_modifies_stop(self, db):
        """
        Position dropped from qty=4 to runner_qty=3 → T1 filled. State
        machine modifies runner stop to entry price and flips state.
        """
        row = _seed_scale_out(db, qty=4, target_qty=1, entry=540.0, stop=534.0)

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = [{"ticker": "SPY", "qty": 3.0}]
        broker.find_open_stop_for_ticker_qty = MagicMock(
            return_value={"order_id": "stop-permId", "stop_price": 534.0, "orderId": 42}
        )
        broker.modify_stop_price = MagicMock(
            return_value={"status": "modified", "order_id": "stop-permId", "new_stop_price": 540.0}
        )

        orch = _make_orch(db)
        with patch("backend.notifications.notify") as mock_notify:
            transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 1
        db.refresh(row)
        assert row.bracket_state == "t1_hit"

        # Runner stop was found for the right qty + ticker
        broker.find_open_stop_for_ticker_qty.assert_called_once_with("SPY", 3.0)
        # Modify was called with the entry price (breakeven)
        broker.modify_stop_price.assert_called_once_with("stop-permId", 540.0)
        # Email fired
        mock_notify.assert_called_once()
        subject = mock_notify.call_args.args[0]
        assert "SPY" in subject and "breakeven" in subject.lower()

    def test_position_zero_transitions_to_closed(self, db):
        """Position at zero → fully exited. Skip stop-move, flip to closed."""
        row = _seed_scale_out(db, qty=4, target_qty=1)

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = []  # no position
        broker.modify_stop_price = MagicMock()

        orch = _make_orch(db)
        transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 1
        db.refresh(row)
        assert row.bracket_state == "closed"
        broker.modify_stop_price.assert_not_called()

    def test_stop_not_found_leaves_state_fresh_for_retry(self, db):
        """
        Position shows T1 filled but find_open_stop returns None (race,
        broker lag, whatever). Leave state=fresh so the next poll retries.
        """
        row = _seed_scale_out(db, qty=4, target_qty=1)

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = [{"ticker": "SPY", "qty": 3.0}]
        broker.find_open_stop_for_ticker_qty = MagicMock(return_value=None)
        broker.modify_stop_price = MagicMock()

        orch = _make_orch(db)
        transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 0
        db.refresh(row)
        assert row.bracket_state == "fresh"
        broker.modify_stop_price.assert_not_called()

    def test_modify_stop_failure_leaves_state_fresh(self, db):
        """Broker-side error on modify → don't flip state, retry next poll."""
        row = _seed_scale_out(db, qty=4, target_qty=1)

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = [{"ticker": "SPY", "qty": 3.0}]
        broker.find_open_stop_for_ticker_qty = MagicMock(
            return_value={"order_id": "stop-1", "stop_price": 534.0, "orderId": 42}
        )
        broker.modify_stop_price = MagicMock(
            return_value={"error": "connection refused", "order_id": "stop-1"}
        )

        orch = _make_orch(db)
        transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 0
        db.refresh(row)
        assert row.bracket_state == "fresh"

    def test_t1_hit_rows_are_not_re_processed(self, db):
        """Only 'fresh' rows are candidates — 't1_hit' and 'closed' are done."""
        _seed_scale_out(db, ticker="SPY", state="t1_hit")
        _seed_scale_out(db, ticker="QQQ", state="closed")

        broker = MagicMock(spec=IBKRBroker)
        broker.get_positions.return_value = [{"ticker": "SPY", "qty": 3.0}]
        broker.find_open_stop_for_ticker_qty = MagicMock()
        broker.modify_stop_price = MagicMock()

        orch = _make_orch(db)
        transitions = orch._advance_scale_out_state_machine(broker)

        assert transitions == 0
        broker.find_open_stop_for_ticker_qty.assert_not_called()

    def test_non_ibkr_broker_is_no_op(self, db):
        """Alpaca scale-out isn't supported — state machine must silently skip."""
        _seed_scale_out(db)
        not_ibkr = MagicMock()  # deliberately not isinstance IBKRBroker
        orch = _make_orch(db)
        assert orch._advance_scale_out_state_machine(not_ibkr) == 0


class TestIBKRFindStop:
    """IBKRBroker.find_open_stop_for_ticker_qty — the runner-stop locator."""

    def test_disconnected_returns_none(self):
        b = IBKRBroker()  # not connected
        assert b.find_open_stop_for_ticker_qty("SPY", 3.0) is None

    def test_matches_open_stop_by_ticker_and_qty(self):
        b = IBKRBroker(paper=True)

        matching_order = SimpleNamespace(
            permId=777,
            orderId=11,
            action="SELL",
            orderType="STP",
            totalQuantity=3,
            auxPrice=534.0,
        )
        matching_trade = SimpleNamespace(
            order=matching_order,
            contract=SimpleNamespace(symbol="SPY"),
            orderStatus=SimpleNamespace(status="Submitted"),
            log=[],
        )
        wrong_qty = SimpleNamespace(
            order=SimpleNamespace(
                permId=778, orderId=12, action="SELL",
                orderType="STP", totalQuantity=1, auxPrice=534.0,
            ),
            contract=SimpleNamespace(symbol="SPY"),
            orderStatus=SimpleNamespace(status="Submitted"),
            log=[],
        )
        wrong_ticker = SimpleNamespace(
            order=SimpleNamespace(
                permId=779, orderId=13, action="SELL",
                orderType="STP", totalQuantity=3, auxPrice=100.0,
            ),
            contract=SimpleNamespace(symbol="QQQ"),
            orderStatus=SimpleNamespace(status="Submitted"),
            log=[],
        )

        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = [wrong_qty, wrong_ticker, matching_trade]
        b._ib = fake_ib

        result = b.find_open_stop_for_ticker_qty("SPY", 3.0)
        assert result is not None
        assert result["order_id"] == "777"
        assert result["stop_price"] == 534.0

    def test_returns_none_on_no_match(self):
        b = IBKRBroker(paper=True)
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = []
        b._ib = fake_ib
        assert b.find_open_stop_for_ticker_qty("SPY", 3.0) is None


class TestIBKRModifyStop:
    """IBKRBroker.modify_stop_price — the breakeven-move primitive."""

    def test_disconnected_returns_error(self):
        b = IBKRBroker()
        result = b.modify_stop_price("permId-1", 540.0)
        assert "error" in result
        assert result["order_id"] == "permId-1"

    def test_not_found_returns_error(self):
        b = IBKRBroker(paper=True)
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = []
        b._ib = fake_ib

        result = b.modify_stop_price("ghost-id", 540.0)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_modifies_auxPrice_and_re_places_order(self):
        """
        Happy path: locate the trade by permId, mutate Order.auxPrice in
        place, call placeOrder with the same order so TWS treats it as
        a modify (not a cancel/replace).
        """
        from unittest.mock import AsyncMock

        b = IBKRBroker(paper=True)
        fake_order = SimpleNamespace(permId=777, orderId=11, auxPrice=534.0)
        fake_trade = SimpleNamespace(
            order=fake_order,
            contract=SimpleNamespace(symbol="SPY"),
            orderStatus=SimpleNamespace(status="Submitted"),
            log=[],
        )
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = [fake_trade]
        fake_ib.placeOrder = MagicMock()
        b._ib = fake_ib

        result = b.modify_stop_price("777", 540.0)
        assert result["status"] == "modified"
        assert result["new_stop_price"] == 540.0
        # auxPrice was mutated BEFORE placeOrder, and placeOrder re-submits
        assert fake_order.auxPrice == 540.0
        fake_ib.placeOrder.assert_called_once_with(fake_trade.contract, fake_order)
