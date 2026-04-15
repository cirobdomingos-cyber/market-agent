"""
BrokerClient — the contract every broker implementation must satisfy.

Methods are deliberately the same shape as the existing AlpacaClient so the
refactor can move callers over one at a time. Anything that returns a dict
returns a *vendor-neutral* dict — both implementations normalise their
provider-specific responses into the same keys.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    """
    Vendor-neutral order outcome.

    Both Alpaca and IBKR implementations build this from their own response
    objects. fill_price is None for orders that were accepted but not yet
    filled (limit orders, after-hours submissions, etc.).
    """
    order_id: str
    ticker: str
    qty: float
    side: str   # buy | sell
    status: str
    is_paper: bool
    fill_price: Optional[float] = None


class BrokerClient(ABC):
    """
    Abstract broker client. Sub-classes implement read + write for one venue.

    All implementations must:
      - Take credentials in __init__ but defer connection until first call
        (so the import doesn't hang on a network round-trip)
      - Return vendor-neutral dicts from the read methods
      - Return OrderResult from place_order
      - Tolerate being constructed without credentials (return error dicts
        from read methods, raise from write methods) so the rest of the app
        can be tested with mocks

    Sub-classes should expose:
      - paper: bool — whether this client is talking to a paper environment
    """

    paper: bool = True

    @abstractmethod
    def is_connected(self) -> bool:
        """
        True when the underlying client/socket is live and ready for use.
        Used by /health for a cheap status check that doesn't trigger an
        actual API round-trip.
        """

    @abstractmethod
    def get_account(self) -> dict:
        """
        Return account summary. Standard keys:
          equity, buying_power, cash, portfolio_value, paper

        Returns {"status": "disconnected", "message": ...} when the broker
        isn't initialised.
        """

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """
        Return open positions. Each dict has these keys:
          ticker, qty, avg_entry, current_price, unrealised_pnl,
          unrealised_pnl_pct

        Returns [] when the broker isn't initialised or there are no positions.
        """

    @abstractmethod
    def get_order_history(self, limit: int = 20) -> list[dict]:
        """
        Return recent orders, most recent first. Each dict has:
          order_id, ticker, qty, filled_qty, side, type, status,
          filled_avg_price, submitted_at, filled_at
        """

    @abstractmethod
    def place_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        paper_only: bool = True,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Submit a market or limit order. paper_only is a runtime safety check —
        when True, implementations MUST refuse to submit if their connection
        is not in paper mode. Defence in depth: even if our /orders/confirm
        endpoint loses its mind, the broker layer enforces the paper rule.

        order_type is "market" (default) or "limit". When "limit", limit_price
        must be provided; implementations raise ValueError otherwise. Limit
        orders are DAY TIF — if not filled by close, they cancel. For GTC
        behaviour use place_bracket_order.
        """

    @abstractmethod
    def place_bracket_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        paper_only: bool = True,
    ) -> OrderResult:
        """
        Submit a bracket entry: a parent LIMIT order with an attached
        take-profit limit and stop-loss stop, all bound as an OCO group at
        the broker. When one exit fills, the other cancels automatically —
        no polling loop on our side, no client-side drift.

        v1 only supports BUY (long) entries. A broker that cannot natively
        express a bracket group MUST raise rather than placing the parent
        without the attached exits — the whole point of this method is the
        guaranteed coupling.

        Returns the OrderResult of the PARENT order. The attached exit legs
        are tracked at the broker and won't appear in our executed_orders
        table until the broker fills them (at which point the existing
        position poll + journal close flow picks them up).
        """

    @abstractmethod
    def close_position(self, ticker: str) -> dict:
        """Close an open position by ticker. Returns the resulting order info."""
