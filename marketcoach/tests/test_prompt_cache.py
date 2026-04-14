"""
Tests for the advisor prompt-cache structure.

Anthropic's prompt cache works by hashing the prefix of the request from
the beginning up to a cache_control marker. Repeated calls with the same
prefix bytes hit the cache and pay ~10% of normal input cost.

These tests are the *correctness boundary* for that mechanism. If they
fail, prompt caching either silently stops working or starts charging
full price for tokens that should have been cached.

What's tested:
  1. Block structure — exactly two blocks, prefix marked ephemeral, suffix not
  2. Cache key stability — same (mode, addendum) inputs produce byte-identical
     prefixes regardless of dynamic content
  3. Cache key separation — different (mode, addendum) combinations produce
     different prefixes (otherwise live and paper would share a cache, which
     is dangerous)
  4. Token sizing — both prefixes are above the Sonnet 1024-token minimum,
     otherwise Anthropic silently skips caching
  5. Dynamic isolation — runtime values never leak into the cached prefix
"""

import pytest

from backend.agents.trading_advisor_agent import (
    DYNAMIC_CONTEXT_TEMPLATE,
    STABLE_INTRO_FRAMEWORK,
    TRADE_PROPOSAL_ADDENDUM,
    _build_system_blocks,
)


def _build(mode="paper", enable_proposals=False, **overrides):
    base = dict(
        mode=mode,
        enable_proposals=enable_proposals,
        account={
            "portfolio_value": 100_000.00,
            "buying_power": 200_000.00,
            "cash": 50_000.00,
        },
        positions=[],
        signals=[],
        theses=[],
    )
    base.update(overrides)
    return _build_system_blocks(**base)


class TestBlockStructure:
    def test_returns_two_blocks(self):
        blocks = _build()
        assert isinstance(blocks, list)
        assert len(blocks) == 2

    def test_first_block_has_cache_control_ephemeral(self):
        blocks = _build()
        assert blocks[0]["type"] == "text"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_second_block_has_no_cache_control(self):
        """The dynamic block MUST NOT be cached — that's the whole point."""
        blocks = _build()
        assert blocks[1]["type"] == "text"
        assert "cache_control" not in blocks[1]


class TestCacheKeyStability:
    """Same (mode, addendum) input → byte-identical prefix → cache hits."""

    def test_same_mode_same_addendum_produces_identical_prefix(self):
        a = _build(mode="paper", enable_proposals=False)
        b = _build(
            mode="paper",
            enable_proposals=False,
            account={
                "portfolio_value": 999_999.99,  # very different account
                "buying_power": 1.00,
                "cash": 0.00,
            },
            positions=[{"ticker": "TSLA", "qty": 100}],
            signals=[{"ticker": "X", "sentiment": "bullish", "headline": "y"}],
        )
        # Prefix bytes MUST match exactly even though the account state differs
        assert a[0]["text"] == b[0]["text"]
        # And the dynamic suffixes MUST differ — that's where the runtime values live
        assert a[1]["text"] != b[1]["text"]

    def test_addendum_enabled_produces_different_prefix(self):
        without = _build(enable_proposals=False)
        with_ = _build(enable_proposals=True)
        assert without[0]["text"] != with_[0]["text"]
        # The addendum should be in the with_ prefix
        assert "trade-proposal" in with_[0]["text"]
        assert "trade-proposal" not in without[0]["text"]

    def test_paper_and_live_produce_different_prefixes(self):
        """Different cache keys for paper vs live is REQUIRED for safety —
        we never want a paper cache hit to influence a live conversation."""
        paper = _build(mode="paper")
        live = _build(mode="live")
        assert paper[0]["text"] != live[0]["text"]
        # Each prefix should contain its own banner
        assert "PAPER TRADING" in paper[0]["text"]
        assert "REAL CAPITAL AT RISK" in live[0]["text"]


class TestCachePrefixSizing:
    """Anthropic Sonnet refuses to cache prefixes shorter than 1024 tokens.
    If we accidentally drop content from the prefix below the threshold the
    cache silently fails — these tests are an early warning."""

    # ~4 chars per token rough estimate; Anthropic's tokeniser is close to this
    SONNET_CACHE_MIN_CHARS = 4096

    def test_paper_prefix_no_addendum_above_minimum(self):
        blocks = _build(mode="paper", enable_proposals=False)
        prefix_len = len(blocks[0]["text"])
        assert prefix_len > self.SONNET_CACHE_MIN_CHARS, (
            f"Stable prefix is {prefix_len} chars — below the ~4096 char "
            "threshold for Sonnet caching. Add content or accept no caching."
        )

    def test_chat_prefix_with_addendum_above_minimum(self):
        blocks = _build(mode="paper", enable_proposals=True)
        prefix_len = len(blocks[0]["text"])
        assert prefix_len > self.SONNET_CACHE_MIN_CHARS

    def test_live_prefix_above_minimum(self):
        blocks = _build(mode="live", enable_proposals=False)
        prefix_len = len(blocks[0]["text"])
        assert prefix_len > self.SONNET_CACHE_MIN_CHARS


class TestDynamicIsolation:
    """Runtime values must NEVER appear in the cached prefix. If they did,
    every call would have a unique cache key and caching would be useless."""

    def test_portfolio_value_only_in_dynamic_block(self):
        blocks = _build(account={"portfolio_value": 12345.67, "buying_power": 0, "cash": 0})
        assert "12,345.67" not in blocks[0]["text"]
        assert "12,345.67" in blocks[1]["text"]

    def test_position_ticker_only_in_dynamic_block(self):
        blocks = _build(positions=[
            {
                "ticker": "TSLA",
                "qty": 50,
                "avg_entry": 200,
                "current_price": 250,
                "unrealised_pnl_pct": 25.0,
            }
        ])
        assert "TSLA" not in blocks[0]["text"]
        assert "TSLA" in blocks[1]["text"]

    def test_signal_headline_only_in_dynamic_block(self):
        blocks = _build(signals=[
            {"ticker": "NVDA", "sentiment": "bullish", "headline": "RAREHEADLINETOKEN"}
        ])
        assert "RAREHEADLINETOKEN" not in blocks[0]["text"]
        assert "RAREHEADLINETOKEN" in blocks[1]["text"]

    def test_thesis_reasoning_only_in_dynamic_block(self):
        blocks = _build(theses=[
            {
                "ticker": "AAPL", "direction": "LONG", "confidence": 0.7,
                "reasoning": "RARETHESISMARKER iPhone cycle hypothesis",
            }
        ])
        assert "RARETHESISMARKER" not in blocks[0]["text"]
        assert "RARETHESISMARKER" in blocks[1]["text"]


class TestSerializableForAnthropic:
    """The blocks must be plain dicts that the Anthropic SDK accepts as the
    `system=` parameter. No SDK type imports here — we rely on the dict form."""

    def test_block_keys_are_anthropic_compatible(self):
        blocks = _build(enable_proposals=True)
        for block in blocks:
            assert "type" in block
            assert "text" in block
            assert block["type"] == "text"
            assert isinstance(block["text"], str)
            assert len(block["text"]) > 0
        # The first block specifically must have the cache marker
        cc = blocks[0].get("cache_control")
        assert cc is not None
        assert cc.get("type") == "ephemeral"
