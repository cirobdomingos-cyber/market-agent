"""
Orchestrator — composes agents into named pipelines.

Pipelines:
  "intelligence"  — NewsAgent -> AnalysisAgent -> thesis resolution (the scheduled 4h run)
  "coach"         — CoachAgent (single turn, stateful via DB)
  "memory"        — MemoryAgent (extract user profile from conversation)
  "full"          — intelligence + memory extraction

The orchestrator owns the Anthropic client and Alpaca client, injecting them
into agents as needed. This keeps agent classes clean of config concerns.
"""

import logging
from typing import Any, Optional

import anthropic
from sqlalchemy.orm import Session

from backend.agents.analysis_agent import AnalysisAgent
from backend.agents.coach_agent import CoachAgent
from backend.agents.memory_agent import MemoryAgent
from backend.agents.news_agent import NewsAgent
from backend.agents.trade_idea_agent import TradeIdeaAgent
from backend.agents.trading_advisor_agent import TradingAdvisorAgent
from backend.agents.base import AgentResult
from backend.config import settings
from backend.db import crud
from backend.brokers import get_broker

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # -- Pipelines -------------------------------------------------------------

    def run_intelligence_pipeline(self, context: dict | None = None) -> dict[str, AgentResult]:
        """
        The core scheduled pipeline: fetch news -> generate theses -> resolve expired.
        Runs every 4 hours via APScheduler.
        """
        # Read the watchlist from the DB (not settings.watchlist, which is
        # only the env-var initial seed). Fall back to settings.watchlist if
        # the DB is empty — should never happen in practice because the
        # lifespan seeds it on first startup, but defensive anyway.
        db_watchlist = [
            row.ticker for row in crud.list_watchlist_tickers(self.db)
        ] or settings.watchlist
        context = context or {"watchlist": db_watchlist}
        results: dict[str, AgentResult] = {}

        logger.info("Orchestrator: starting intelligence pipeline")

        # Step 1 — fetch and extract news signals
        news_agent = NewsAgent(
            self.db,
            self.client,
            cache_ttl_minutes=settings.news_cache_ttl_minutes,
        )
        news_result = news_agent.run(context)
        results["news"] = news_result

        if not news_result.success:
            logger.warning("News agent failed — skipping analysis: %s", news_result.error)
            return results

        # Step 2 — generate theses from signals
        analysis_agent = AnalysisAgent(self.db, self.client)
        analysis_result = analysis_agent.run(context)
        results["analysis"] = analysis_result

        # Step 3 — resolve any expired theses against actual prices
        resolved_count = analysis_agent.resolve_expired_theses()
        results["resolution"] = AgentResult(
            success=True,
            data={"resolved": resolved_count},
        )

        # Step 4 — generate trade ideas from theses
        if analysis_result.success and analysis_result.data.get("theses"):
            # Get portfolio value from Alpaca if available
            portfolio_value = 100_000.0  # default
            broker = get_broker()
            existing_positions = []
            if broker:
                account = broker.get_account()
                if "portfolio_value" in account:
                    portfolio_value = account["portfolio_value"]
                existing_positions = broker.get_positions()

            # Get user risk tolerance from memories
            risk_tolerance = "moderate"
            risk_mem = crud.get_memory(self.db, "risk_tolerance")
            if risk_mem:
                val = risk_mem.value.lower()
                if val in ("conservative", "moderate", "aggressive"):
                    risk_tolerance = val

            trade_agent = TradeIdeaAgent(
                self.db, self.client, portfolio_value=portfolio_value,
            )
            trade_result = trade_agent.run({
                "theses": analysis_result.data["theses"],
                "portfolio_value": portfolio_value,
                "risk_tolerance": risk_tolerance,
                "existing_positions": existing_positions,
            })
            results["trade_ideas"] = trade_result
        else:
            results["trade_ideas"] = AgentResult(
                success=True,
                data={"trade_ideas": [], "skipped": [], "reason": "no theses to process"},
            )

        # Step 5 — auto-generate news reactions for high-impact signals
        reactions_created = self._trigger_news_reactions(
            news_result.data.get("signals", []) if news_result.success else []
        )
        results["news_reactions"] = AgentResult(
            success=True,
            data={"created": reactions_created},
        )

        logger.info(
            "Orchestrator: intelligence pipeline complete. "
            "signals=%s theses=%s resolved=%s trade_ideas=%s reactions=%s",
            len(news_result.data.get("signals", [])) if news_result.success else 0,
            len(analysis_result.data.get("theses", [])) if analysis_result.success else 0,
            resolved_count,
            len(results["trade_ideas"].data.get("trade_ideas", [])),
            reactions_created,
        )
        return results

    # ── News reactions ───────────────────────────────────────────────────────

    _TRIGGER_PRIORITY = {"position": 0, "thesis": 1, "watchlist": 2}

    def _classify_reaction_trigger(
        self,
        ticker: str,
        position_tickers: set[str],
        thesis_tickers: set[str],
        watchlist_tickers: set[str],
    ) -> Optional[str]:
        """Return the highest-priority trigger reason for this ticker, or None
        if it doesn't match any user-relevant set (and should be skipped)."""
        if ticker in position_tickers:
            return "position"
        if ticker in thesis_tickers:
            return "thesis"
        if ticker in watchlist_tickers:
            return "watchlist"
        return None

    def _trigger_news_reactions(self, signals: list[dict]) -> int:
        """
        Filter signals for high-impact events, dedupe, rate-limit, and
        auto-generate TradingAdvisor reactions. Returns number of reactions
        created (successful + failed).

        The filter has three gates:
          1. Impact: confidence >= threshold AND sentiment is directional
          2. Relevance: ticker affects a position, open thesis, or watchlist
          3. Dedupe: no existing reaction for this ticker in the last N hours
        Then sorted by (relevance priority, confidence desc) and capped by
        settings.news_reactions_per_run_max to prevent runaway spend.
        """
        if not settings.news_reactions_enabled or not signals:
            return 0

        # Gate 1 — impact filter
        impactful = [
            s for s in signals
            if s.get("confidence", 0) >= settings.news_reaction_min_confidence
            and s.get("sentiment") in ("bullish", "bearish")
        ]
        if not impactful:
            logger.info(
                "News reactions: 0 impactful signals (threshold=%.2f)",
                settings.news_reaction_min_confidence,
            )
            return 0

        # Gate 2 — relevance classification
        broker = get_broker()
        position_tickers: set[str] = set()
        if broker:
            position_tickers = {
                (p.get("ticker") or "").upper() for p in broker.get_positions()
            }
        thesis_tickers = {
            (t.ticker or "").upper() for t in crud.get_open_theses(self.db, limit=100)
        }
        watchlist_tickers = crud.get_watchlist_tickers_set(self.db)

        candidates: list[tuple[dict, str, str]] = []
        for sig in impactful:
            ticker = (sig.get("ticker") or "").upper()
            if not ticker:
                continue
            reason = self._classify_reaction_trigger(
                ticker, position_tickers, thesis_tickers, watchlist_tickers
            )
            if reason is None:
                continue
            # Gate 3 — dedupe window
            if crud.has_recent_reaction_for_ticker(
                self.db, ticker, settings.news_reaction_dedupe_hours
            ):
                logger.debug("News reactions: dedupe skip for %s", ticker)
                continue
            candidates.append((sig, ticker, reason))

        if not candidates:
            return 0

        # Sort by (priority, -confidence) and cap
        candidates.sort(
            key=lambda c: (
                self._TRIGGER_PRIORITY[c[2]],
                -float(c[0].get("confidence", 0)),
            )
        )
        candidates = candidates[: settings.news_reactions_per_run_max]

        created = 0
        for sig, ticker, reason in candidates:
            headline = sig.get("headline", "")
            reaction_prompt = (
                f"React to this news on {ticker}: {headline}\n\n"
                "Assess impact on any open positions and call out new "
                "opportunities this creates. Be specific about urgency — what "
                "needs action before next market open?"
            )
            try:
                advisor_result = self.run_advisor({
                    "session_id": settings.news_reaction_session_id,
                    "user_message": reaction_prompt,
                })
                content = (
                    advisor_result.data.get("reply", "")
                    if advisor_result.data
                    else ""
                )
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=headline,
                    content=content,
                    trigger_reason=reason,
                    status="unread" if advisor_result.success else "failed",
                    error=advisor_result.error,
                )
                created += 1

                if advisor_result.success and content:
                    from backend.notifications import notify
                    notify(
                        f"News Alert: {ticker} — {headline[:80]}",
                        f"<h3>{ticker}: {headline}</h3><p>{content[:2000]}</p>",
                        f"{ticker}: {headline}\n\n{content[:2000]}",
                    )
            except Exception as exc:
                logger.warning(
                    "News reaction generation failed for %s: %s", ticker, exc
                )
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=headline,
                    content="",
                    trigger_reason=reason,
                    status="failed",
                    error=str(exc),
                )
                created += 1

        logger.info("News reactions: created %d reactions", created)
        return created

    # ── Position change reviews ──────────────────────────────────────────────

    _POSITION_REVIEW_PROMPTS = {
        "position_opened": (
            "You just opened a new position: {qty} shares of {ticker} at "
            "${avg_entry}. Review this trade. At this entry, is the thesis "
            "still intact? What stop loss and price targets would you set "
            "right now? Is this position size appropriate given the 20% "
            "concentration cap and your other holdings?"
        ),
        "position_closed": (
            "Your position in {ticker} was closed (was {prev_qty} shares "
            "@ ${prev_avg}, last seen at ${prev_price}). Review the outcome — "
            "was this a planned exit (target hit, stop hit) or unplanned "
            "(panic close, broker action)? What does this trade teach you "
            "about the original entry decision?"
        ),
        "position_changed": (
            "Your position in {ticker} changed: was {prev_qty} shares, now "
            "{qty} shares (avg entry ${avg_entry}). Review the change — was "
            "this a planned add/trim or unplanned? Is the new size still "
            "appropriate for the thesis?"
        ),
        "pnl_threshold": (
            "Your {ticker} position has moved significantly: was {prev_pnl_pct}%, "
            "now {pnl_pct}% (avg entry ${avg_entry}, current ${current_price}). "
            "Review whether the original thesis is still intact, whether to "
            "add / trim / hold / exit, and where your stop loss should be in "
            "light of this move. Be specific with price levels."
        ),
    }

    _POSITION_REVIEW_PRIORITY = {
        "position_opened": 0,
        "position_closed": 1,
        "pnl_threshold": 2,
        "position_changed": 3,
    }

    def _classify_position_change(
        self,
        ticker: str,
        current: dict | None,
        previous,  # PositionSnapshot or None
    ) -> tuple[str | None, dict]:
        """
        Compare the current broker position to the previous snapshot for one
        ticker. Return (trigger_reason, prompt_args) or (None, {}) if no
        meaningful change. prompt_args is filled in for the Anthropic prompt.
        """
        from backend.config import settings as _settings

        cur_qty = float(current.get("qty") or 0) if current else 0.0
        prev_qty = float(previous.qty) if previous else 0.0

        cur_avg = float(current.get("avg_entry") or 0) if current else 0.0
        prev_avg = float(previous.avg_entry) if previous and previous.avg_entry else 0.0

        cur_price = float(current.get("current_price") or 0) if current else 0.0
        cur_pnl_pct = float(current.get("unrealised_pnl_pct") or 0) if current else 0.0
        prev_pnl_pct = (
            float(previous.unrealised_pnl_pct) if previous and previous.unrealised_pnl_pct is not None else 0.0
        )

        base_args = {
            "ticker": ticker,
            "qty": cur_qty,
            "avg_entry": f"{cur_avg:.2f}",
            "current_price": f"{cur_price:.2f}",
            "pnl_pct": f"{cur_pnl_pct:.2f}",
            "prev_qty": prev_qty,
            "prev_avg": f"{prev_avg:.2f}",
            "prev_price": f"{(float(previous.current_price) if previous and previous.current_price else 0):.2f}",
            "prev_pnl_pct": f"{prev_pnl_pct:.2f}",
        }

        # Case 1: position opened (no previous, OR previous qty was 0 and now != 0)
        if cur_qty != 0 and prev_qty == 0:
            return "position_opened", base_args

        # Case 2: position closed (previous qty != 0, current qty == 0)
        if cur_qty == 0 and prev_qty != 0:
            return "position_closed", base_args

        # Both sides hold the position — check for meaningful changes
        if cur_qty != 0 and prev_qty != 0:
            qty_change_pct = abs(cur_qty - prev_qty) / abs(prev_qty) * 100
            if qty_change_pct >= _settings.position_qty_change_threshold_pct:
                return "position_changed", base_args

            # P&L move check — measured in percentage POINTS, not relative
            # change. e.g., position went from +2% to +13% is an 11pp move.
            pnl_move_pp = abs(cur_pnl_pct - prev_pnl_pct)
            if pnl_move_pp >= _settings.position_pnl_change_threshold_pct:
                return "pnl_threshold", base_args

        return None, {}

    _POSITION_POLL_INIT_KEY = "position_poll_initialised"

    def _check_price_alerts(self) -> int:
        """
        Check all active price alerts against current prices. Fire a
        news_reactions notification for each alert whose condition is met,
        and mark the alert inactive so it doesn't re-fire on the next poll.

        One yfinance quote per unique ticker (not per alert) — alerts on
        the same ticker share a quote.

        Returns the number of alerts fired.
        """
        grouped = crud.get_active_alerts_grouped_by_ticker(self.db)
        if not grouped:
            return 0

        from backend.tools.market_data import execute_market_data
        fired = 0

        for ticker, alerts in grouped.items():
            try:
                quote = execute_market_data(action="quote", ticker=ticker)
            except Exception as exc:
                logger.warning("Alert quote lookup failed for %s: %s", ticker, exc)
                continue
            if not isinstance(quote, dict) or not quote.get("price"):
                continue
            current_price = float(quote["price"])

            for alert in alerts:
                target = float(alert.target_price)
                condition = alert.condition
                hit = False
                if condition == "above" and current_price >= target:
                    hit = True
                elif condition == "below" and current_price <= target:
                    hit = True

                if not hit:
                    continue

                # Compose a readable headline for the notification
                direction = "above" if condition == "above" else "below"
                headline = (
                    f"{ticker} {direction} ${target:.2f} — now ${current_price:.2f}"
                )
                content_lines = [
                    f"**Price alert fired:** {ticker} {direction} ${target:.2f}",
                    "",
                    f"- **Current price:** ${current_price:.2f}",
                    f"- **Target:** ${target:.2f}",
                    f"- **Condition:** {condition}",
                ]
                if alert.note:
                    content_lines.append(f"- **Your note:** {alert.note}")
                content_lines.append("")
                content_lines.append(
                    "Set from the Portfolio → Alerts section. This alert "
                    "has been deactivated — create a new one if you want "
                    "to watch the same level again."
                )
                content = "\n".join(content_lines)

                try:
                    crud.create_news_reaction(
                        self.db,
                        ticker=ticker,
                        headline=headline,
                        content=content,
                        trigger_reason="price_alert",
                        status="unread",
                    )
                    crud.mark_alert_triggered(self.db, alert.id, current_price)
                    fired += 1
                    logger.info(
                        "Price alert fired: %s %s $%.2f (current $%.2f)",
                        ticker, direction, target, current_price,
                    )

                    from backend.notifications import notify
                    notify(
                        f"Price Alert: {ticker} hit ${current_price:.2f}",
                        f"<h3>{headline}</h3><p>{content}</p>",
                        f"{headline}\n\n{content}",
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to fire alert for %s: %s", ticker, exc
                    )

        return fired

    def _snapshot_equity(self, broker) -> None:
        """
        Write one equity_snapshots row using the current broker account.
        Called once per poll cycle. Any failure here is logged and
        swallowed — this is telemetry, not critical path.
        """
        try:
            account = broker.get_account()
        except Exception as exc:
            logger.warning("Equity snapshot: get_account failed: %s", exc)
            return
        if not isinstance(account, dict):
            return
        # Disconnected / error responses have no equity field
        equity = account.get("equity")
        if equity is None:
            return
        try:
            crud.create_equity_snapshot(
                self.db,
                equity=float(equity),
                buying_power=(
                    float(account.get("buying_power"))
                    if account.get("buying_power") is not None
                    else None
                ),
                cash=(
                    float(account.get("cash"))
                    if account.get("cash") is not None
                    else None
                ),
                portfolio_value=(
                    float(account.get("portfolio_value"))
                    if account.get("portfolio_value") is not None
                    else None
                ),
            )
        except Exception as exc:
            logger.warning("Equity snapshot: DB write failed: %s", exc)

    def _check_position_changes(self) -> int:
        """
        Poll the broker for current positions, diff against the latest stored
        snapshot per ticker, generate advisor reviews for meaningful changes,
        and persist the new snapshot. Returns the number of reviews created.

        First-poll behaviour: if this is the very first time the poll has
        ever run, write the baseline silently and return 0. We don't want
        every existing position to look "newly opened" the first time the
        polling job runs.

        We track the "first poll done" state via a UserMemory key rather
        than the snapshot table itself because an empty broker (no positions)
        would write zero rows on the first poll, causing the next poll to
        also be treated as the first.
        """
        from backend.config import settings as _settings

        if not _settings.position_reviews_enabled:
            return 0

        # Check price alerts BEFORE the broker guard — alerts use
        # yfinance for quotes (not the broker), so they work on Railway
        # where BROKER_PROVIDER=none. Without this ordering, all price
        # alerts silently never fire on cloud-hosted backends.
        try:
            self._check_price_alerts()
        except Exception as exc:
            logger.warning("Price alert check failed: %s", exc)

        broker = get_broker()
        if broker is None:
            logger.debug("Position poll: no broker initialised, skipping")
            return 0

        # Snapshot account equity regardless of position changes. This
        # runs on every poll (including the first) so the Dashboard's
        # equity chart has a data point every 5 minutes. Failures are
        # non-fatal — we log and continue so equity-write issues can't
        # break the position review pipeline.
        self._snapshot_equity(broker)

        try:
            current_positions = broker.get_positions()
        except Exception as exc:
            logger.warning("Position poll: broker.get_positions failed: %s", exc)
            return 0

        init_marker = crud.get_memory(self.db, self._POSITION_POLL_INIT_KEY)
        is_first_poll = init_marker is None
        previous = crud.get_latest_position_snapshots(self.db)

        # Index current by ticker
        current_by_ticker: dict[str, dict] = {}
        for p in current_positions:
            ticker = (p.get("ticker") or "").upper()
            if ticker:
                current_by_ticker[ticker] = p

        # Tickers to consider: union of current + previous (so closes are detected)
        all_tickers = set(current_by_ticker.keys()) | set(previous.keys())

        # First poll: just establish the baseline, no reviews
        if is_first_poll:
            self._write_position_snapshots(current_by_ticker, previous_keys=set())
            crud.upsert_memory(
                self.db,
                key=self._POSITION_POLL_INIT_KEY,
                value="true",
                category="general",
            )
            logger.info(
                "Position poll: first run, wrote baseline of %d positions",
                len(current_by_ticker),
            )
            return 0

        # IMPORTANT: update the journal BEFORE running the review dedupe.
        # Journal writes are cheap (no Anthropic call) and represent the
        # ground truth of the position lifecycle — they must happen on
        # every meaningful change, independent of review cost controls.
        #
        # The old version of this code ran _update_journal_from_changes AFTER
        # the dedupe early-return, which meant a recent position_opened
        # reaction would silently swallow the subsequent position_closed
        # journal update when the user closed the trade within the dedupe
        # window. Symptom: journal stuck on "open" even after the position
        # actually closed.
        self._update_journal_from_changes(all_tickers, current_by_ticker, previous)

        # Classify each ticker's change for REVIEW GENERATION. Dedupe and
        # per-poll cap apply only to reviews (which cost Anthropic tokens),
        # not to the journal updates above.
        candidates: list[tuple[str, str, dict]] = []  # (ticker, reason, prompt_args)
        for ticker in all_tickers:
            current = current_by_ticker.get(ticker)
            prev = previous.get(ticker)
            reason, args = self._classify_position_change(ticker, current, prev)
            if reason is None:
                continue
            # Dedupe: skip if a reaction for this ticker exists in the window
            if crud.has_recent_reaction_for_ticker(
                self.db, ticker, _settings.position_review_dedupe_hours
            ):
                logger.debug("Position review: dedupe skip for %s", ticker)
                continue
            candidates.append((ticker, reason, args))

        if not candidates:
            # Always write the new snapshot even when nothing changed — keeps
            # the most-recent timestamp moving so we know the poll is alive
            self._write_position_snapshots(current_by_ticker, set(previous.keys()))
            return 0

        # Sort by priority and cap
        candidates.sort(key=lambda c: self._POSITION_REVIEW_PRIORITY[c[1]])
        candidates = candidates[: _settings.position_reviews_per_poll_max]

        created = 0
        for ticker, reason, args in candidates:
            prompt_template = self._POSITION_REVIEW_PROMPTS[reason]
            user_message = prompt_template.format(**args)

            try:
                advisor_result = self.run_advisor({
                    "session_id": _settings.position_review_session_id,
                    "user_message": user_message,
                    # Reviews are read-only analysis, never click-to-execute
                    "enable_trade_proposals": False,
                })
                content = (
                    advisor_result.data.get("reply", "")
                    if advisor_result.data
                    else ""
                )
                # Reuse the news_reactions table — same UI surface, different category
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=f"Position change: {reason.replace('_', ' ')}",
                    content=content,
                    trigger_reason=reason,
                    status="unread" if advisor_result.success else "failed",
                    error=advisor_result.error,
                )
                created += 1
            except Exception as exc:
                logger.warning(
                    "Position review generation failed for %s: %s", ticker, exc
                )
                crud.create_news_reaction(
                    self.db,
                    ticker=ticker,
                    headline=f"Position change: {reason.replace('_', ' ')}",
                    content="",
                    trigger_reason=reason,
                    status="failed",
                    error=str(exc),
                )
                created += 1

        # Always persist the new snapshot after processing
        self._write_position_snapshots(current_by_ticker, set(previous.keys()))

        logger.info("Position poll: created %d reviews", created)
        return created

    def _update_journal_from_changes(
        self,
        all_tickers: set[str],
        current_by_ticker: dict[str, dict],
        previous,  # dict[str, PositionSnapshot]
    ) -> None:
        """
        Reconcile the trade journal with the latest broker snapshot.

        Two cases:
          1. A new ticker appears with no matching open journal entry →
             this is a manual trade placed directly in Alpaca/IBKR. Create
             a pending journal entry (no thesis yet — the user fills it in
             after the fact via the Journal UI).

          2. A previously-held ticker disappears (qty went to 0) → the
             position closed. Find the matching open journal entry and fill
             in the close fields. The user still needs to add their lesson
             before the entry is "complete".

        Both cases are best-effort. If the journal write fails for any
        reason we log and continue — the polling job's main purpose
        (generating reviews) must not be blocked by journal hiccups.
        """
        from datetime import datetime, timezone

        for ticker in all_tickers:
            current = current_by_ticker.get(ticker)
            prev = previous.get(ticker)
            cur_qty = float(current.get("qty") or 0) if current else 0.0
            prev_qty = float(prev.qty) if prev else 0.0

            try:
                # Case 1: new position (or qty went from 0 to non-zero)
                if cur_qty != 0 and prev_qty == 0:
                    existing = crud.get_open_journal_entry_for_ticker(self.db, ticker)
                    if existing is None:
                        # No matching journal entry → manual trade. Create
                        # one with no thesis so the UI can prompt for it.
                        crud.create_journal_entry(
                            self.db,
                            ticker=ticker,
                            side="buy",  # assume buys for opens
                            qty=cur_qty,
                            open_price=float(current.get("avg_entry") or 0),
                            opened_at=datetime.now(timezone.utc),
                            user_thesis=None,  # ← needs_thesis=True in the UI
                            status="open",
                        )
                        logger.info(
                            "Journal: created pending entry for manual trade on %s",
                            ticker,
                        )

                # Case 2: position closed
                elif cur_qty == 0 and prev_qty != 0:
                    open_entry = crud.get_open_journal_entry_for_ticker(self.db, ticker)
                    if open_entry is not None:
                        # Close-price lookup priority (ground truth first):
                        #
                        #   1. The actual SELL fill from executed_orders
                        #      — the authoritative price the broker gave us
                        #      when the order executed. This is what really
                        #      happened and should always win when available.
                        #
                        #   2. The previous snapshot's current_price — valid
                        #      for Alpaca (which fills current_price in
                        #      get_positions). IBKR paper returns None here
                        #      because we don't pay for real-time data.
                        #
                        #   3. A yfinance quote via the market_data tool —
                        #      approximation for brokers that can't supply
                        #      real-time quotes. Accurate to within a few
                        #      minutes, not a few cents.
                        #
                        #   4. The open_price as a break-even fallback —
                        #      better than recording a phantom -100% if all
                        #      other lookups fail.
                        close_price = 0.0
                        sell_fill = crud.find_latest_sell_fill(
                            self.db,
                            ticker,
                            after_dt=open_entry.opened_at,
                        )
                        if sell_fill and sell_fill.fill_price:
                            close_price = float(sell_fill.fill_price)
                            logger.info(
                                "Journal close for %s: using executed_orders fill $%.4f",
                                ticker, close_price,
                            )
                        if not close_price and prev and prev.current_price:
                            close_price = float(prev.current_price)
                        if not close_price:
                            try:
                                from backend.tools.market_data import execute_market_data
                                quote = execute_market_data(action="quote", ticker=ticker)
                                if isinstance(quote, dict) and quote.get("price"):
                                    close_price = float(quote["price"])
                                    logger.info(
                                        "Journal close for %s: using yfinance quote $%.4f "
                                        "(no executed_orders sell found)",
                                        ticker, close_price,
                                    )
                            except Exception as exc:
                                logger.debug("Close price yfinance fallback failed for %s: %s", ticker, exc)
                        if not close_price:
                            close_price = float(open_entry.open_price or 0)
                        open_price = float(open_entry.open_price or 0)
                        pnl_pct = (
                            ((close_price - open_price) / open_price * 100)
                            if open_price > 0
                            else None
                        )
                        pnl_amount = (
                            (close_price - open_price) * float(open_entry.qty)
                            if open_price > 0
                            else None
                        )
                        # Was the buy direction profitable? Buys profit on rise.
                        advised_profitable = (
                            close_price > open_price
                            if open_entry.side == "buy"
                            else close_price < open_price
                        )
                        # SQLite stores DateTime as naive — normalize both
                        # sides to naive UTC before subtracting to avoid
                        # "can't subtract offset-naive and offset-aware".
                        if open_entry.opened_at:
                            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                            opened_naive = (
                                open_entry.opened_at.replace(tzinfo=None)
                                if open_entry.opened_at.tzinfo
                                else open_entry.opened_at
                            )
                            days_held = (now_naive - opened_naive).days
                        else:
                            days_held = None
                        crud.update_journal_entry(
                            self.db,
                            open_entry.id,
                            close_price=close_price,
                            closed_at=datetime.now(timezone.utc),
                            pnl_amount=pnl_amount,
                            pnl_pct=pnl_pct,
                            days_held=days_held,
                            advised_direction_profitable=advised_profitable,
                            status="closed",
                        )
                        logger.info(
                            "Journal: closed entry for %s (P&L %.2f%%)",
                            ticker,
                            pnl_pct or 0,
                        )
            except Exception as exc:
                logger.warning(
                    "Journal update failed for %s: %s — continuing", ticker, exc
                )

    def _write_position_snapshots(
        self,
        current_by_ticker: dict[str, dict],
        previous_keys: set[str],
    ) -> None:
        """
        Persist a snapshot row for every current position, plus a qty=0 row
        for any ticker that was previously held but is no longer present
        (so the next diff knows the close was already handled).
        """
        rows: list[dict] = []
        for ticker, p in current_by_ticker.items():
            rows.append({
                "ticker": ticker,
                "qty": float(p.get("qty") or 0),
                "avg_entry": (
                    float(p.get("avg_entry")) if p.get("avg_entry") is not None else None
                ),
                "current_price": (
                    float(p.get("current_price"))
                    if p.get("current_price") is not None
                    else None
                ),
                "unrealised_pnl_pct": (
                    float(p.get("unrealised_pnl_pct"))
                    if p.get("unrealised_pnl_pct") is not None
                    else None
                ),
            })

        # Tombstone rows for closed positions
        for ticker in previous_keys - set(current_by_ticker.keys()):
            rows.append({
                "ticker": ticker,
                "qty": 0.0,
                "avg_entry": None,
                "current_price": None,
                "unrealised_pnl_pct": None,
            })

        if rows:
            crud.create_position_snapshots(self.db, rows)

    def run_coach(self, context: dict) -> AgentResult:
        """
        Single coach turn. Context must include session_id and user_message.
        Enriches context with Alpaca portfolio data if available.
        """
        # Inject live portfolio from Alpaca if configured
        broker = get_broker()
        if broker:
            context.setdefault("portfolio", broker.get_positions())

        coach = CoachAgent(self.db, self.client)
        result = coach.run(context)

        # Fire-and-forget memory extraction from this conversation
        # This runs after every coach turn to keep user profile up to date
        try:
            session_id = context.get("session_id", "default")
            messages = crud.get_session_messages(self.db, session_id)
            if len(messages) >= 4:  # only extract after a meaningful exchange
                recent = [{"role": m.role, "content": m.content} for m in messages[-10:]]
                memory_agent = MemoryAgent(self.db, self.client)
                memory_result = memory_agent.run({
                    "action": "extract",
                    "session_id": session_id,
                    "messages": recent,
                })
                if memory_result.success and memory_result.data.get("extracted", 0) > 0:
                    logger.info(
                        "MemoryAgent extracted %d memories from session %s",
                        memory_result.data["extracted"],
                        session_id,
                    )
        except Exception as exc:
            # Memory extraction is best-effort — never block the coach response
            logger.warning("Memory extraction failed (non-blocking): %s", exc)

        return result

    def run_advisor(self, context: dict) -> AgentResult:
        """
        Single trading-advisor turn. Context must include session_id and
        user_message. Enriches context with live Alpaca account data and
        positions so the prompt can ground position sizing in real numbers.
        """
        broker = get_broker()
        if broker:
            context.setdefault("account", broker.get_account())
            context.setdefault("positions", broker.get_positions())
        else:
            context.setdefault("account", {})
            context.setdefault("positions", [])

        advisor = TradingAdvisorAgent(self.db, self.client)
        return advisor.run(context)

    def run_weekly_plan(self, trigger: str = "scheduled") -> AgentResult:
        """
        Generate and persist a weekly trading plan via the TradingAdvisorAgent.

        This is the entry point for both the Sunday-night cron job and the
        manual "run now" button in the UI. The generated plan is stored in the
        weekly_plans table so the frontend can display the latest one without
        re-invoking Claude.
        """
        from backend.config import settings as _settings

        session_id = _settings.weekly_plan_session_id

        theses = crud.get_open_theses(self.db, limit=10)
        signals = crud.get_recent_signals(self.db, hours=168)  # last 7 days

        context = {
            "session_id": session_id,
            "user_message": (
                "Weekly plan. Run Layer 1–2 macro + sector analysis, list this "
                "week's key events (earnings, Fed, econ data), and give specific "
                "trades to watch with entry levels. Include management notes for "
                "any existing positions."
            ),
            "theses": [
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "confidence": t.confidence,
                    "timeframe": t.timeframe,
                    "reasoning": (t.reasoning or "")[:200],
                }
                for t in theses
            ],
            "signals": [
                {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
                for s in signals[:20]
            ],
        }

        result = self.run_advisor(context)

        reply = result.data.get("reply", "") if result.data else ""
        crud.create_weekly_plan(
            self.db,
            session_id=session_id,
            content=reply,
            status="completed" if result.success else "failed",
            error=result.error,
            trigger=trigger,
        )

        logger.info(
            "Weekly plan generated (trigger=%s, success=%s, length=%d chars)",
            trigger,
            result.success,
            len(reply),
        )
        return result

    def run_morning_brief(self, trigger: str = "scheduled") -> AgentResult:
        """
        Generate and persist a daily pre-market brief via the TradingAdvisor.

        Tighter, shorter, and more time-sensitive than the weekly plan.
        Designed to be read on a phone in bed before market open.
        """
        from backend.config import settings as _settings

        session_id = _settings.morning_brief_session_id

        theses = crud.get_open_theses(self.db, limit=10)
        signals = crud.get_recent_signals(self.db, hours=24)  # last 24h

        # The advisor knows about its modes — give it a focused user message
        # rather than the canonical "react to" or "weekly plan" trigger so the
        # output stays short and pre-market-oriented instead of running the
        # full Layer 1–4 framework.
        user_message = (
            "Generate today's morning brief. Keep it SHORT and scannable — "
            "I'm reading this on my phone before the open. Use these sections "
            "with bullet points (no long paragraphs):\n\n"
            "1. **Overnight** — Asia/Europe close, US futures direction, any "
            "macro shock from the last 12 hours.\n"
            "2. **Earnings today** — who reports premarket vs after the bell. "
            "Flag any in my positions or watchlist.\n"
            "3. **Premarket movers** — any of my positions or watchlist tickers "
            "gapping up/down and why.\n"
            "4. **Key events today** — Fed speakers, economic data releases, "
            "FDA decisions, anything that could move the tape.\n"
            "5. **Action items** — specific things to watch for at the open. "
            "Be concrete: 'NVDA: watch for hold above $450, stop $442'. If "
            "there's nothing to do, say 'No actions — sit on hands today'.\n\n"
            "Skip Layer 1–2 framework boilerplate. Skip the 'risk reminder' "
            "footer. Get straight to what I need to know."
        )

        context = {
            "session_id": session_id,
            "user_message": user_message,
            "theses": [
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "confidence": t.confidence,
                    "timeframe": t.timeframe,
                    "reasoning": (t.reasoning or "")[:200],
                }
                for t in theses
            ],
            "signals": [
                {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
                for s in signals[:15]
            ],
        }

        result = self.run_advisor(context)

        reply = result.data.get("reply", "") if result.data else ""
        crud.create_morning_brief(
            self.db,
            session_id=session_id,
            content=reply,
            status="completed" if result.success else "failed",
            error=result.error,
            trigger=trigger,
        )

        logger.info(
            "Morning brief generated (trigger=%s, success=%s, length=%d chars)",
            trigger,
            result.success,
            len(reply),
        )
        return result

    def get_user_profile(self) -> dict:
        """Return the user's stored memories/preferences."""
        memory_agent = MemoryAgent(self.db, self.client)
        result = memory_agent.run({"action": "read"})
        return result.data if result.success else {}

    def get_pipeline_status(self, results: dict[str, AgentResult]) -> dict[str, Any]:
        """Summarise pipeline results for the API response."""
        return {
            agent: {
                "success": r.success,
                "error": r.error,
                "run_at": r.run_at.isoformat() if r.run_at else None,
            }
            for agent, r in results.items()
        }
