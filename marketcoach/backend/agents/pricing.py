"""
Static Anthropic price book + cost calculator for AgentCall rows.

Prices are list price per million tokens, in USD. Sourced from
anthropic.com pricing as of 2026-04. Update when new models ship.

Why store list price (and not real cost): the user runs on a Pro/Max
plan, where calls are bundled. There is no per-call invoice. List price
is an upper-bound proxy that lets the dashboard compare relative spend
between agents, day-over-day trends, and the value of cache hits — all
of which are the actual analytical questions, not "what's my Anthropic
bill" (the bill is flat).

Cache pricing convention used here:
  - cache_read_input_tokens are billed at ~10% of normal input
  - cache_creation_input_tokens are billed at ~125% of normal input
  - input_tokens (uncached) are billed at 100%
  - output_tokens at the output rate
"""

from typing import Dict, NamedTuple


class _Price(NamedTuple):
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# Per million tokens. Add new models here when adopted.
# Unknown models fall back to the Sonnet entry — safest assumption is
# that an unrecognized model is not cheaper than Sonnet.
_PRICES: Dict[str, _Price] = {
    "claude-sonnet-4-6":         _Price(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5-20251001": _Price(1.00,  5.00, 0.10, 1.25),
    "claude-opus-4-7":           _Price(15.00, 75.00, 1.50, 18.75),
    "claude-opus-4-6":           _Price(15.00, 75.00, 1.50, 18.75),
}

_FALLBACK = _PRICES["claude-sonnet-4-6"]


def cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """
    Compute list-price USD cost of one Anthropic call. Returns 0.0 when
    every counter is zero (mocked test responses) so the row still gets
    written but nothing skews the total.
    """
    p = _PRICES.get(model, _FALLBACK)
    return round(
        (input_tokens          * p.input_per_mtok       / 1_000_000) +
        (output_tokens         * p.output_per_mtok      / 1_000_000) +
        (cache_read_tokens     * p.cache_read_per_mtok  / 1_000_000) +
        (cache_creation_tokens * p.cache_write_per_mtok / 1_000_000),
        6,
    )
