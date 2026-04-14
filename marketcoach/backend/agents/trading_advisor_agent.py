"""
TradingAdvisorAgent — decisive AI trading advisor for the portfolio owner.

Unlike CoachAgent (which teaches and refuses direct recommendations), this
agent is built for the account owner's own research: it takes a side, sizes
positions against a real Alpaca paper account, and enforces the 2% / 20%
risk rules in the prompt itself.

It reuses the same _agentic_loop pattern as CoachAgent with web_search and
market_data tools. Capital, positions, and buying power are injected live
from Alpaca into the system prompt — that grounds position sizing in real
numbers instead of placeholders.

Modes (auto-detected from user message):
  SCAN             — "scan the market"
  ANALYZE <TICKER> — "analyze AAPL"
  PORTFOLIO REVIEW — "review portfolio"
  NEWS REACT       — "react to <event>"
  WEEKLY PLAN      — "weekly plan"
"""

import logging

import anthropic
from sqlalchemy.orm import Session

from backend.agents.base import AgentResult, BaseAgent
from backend.config import settings
from backend.db import crud
from backend.tools.alpaca import ALPACA_READ_TOOL, execute_alpaca_read_tool
from backend.tools.market_data import MARKET_DATA_TOOL, execute_market_data
from backend.tools.web_search import WEB_SEARCH_TOOL, execute_web_search

logger = logging.getLogger(__name__)

HISTORY_WINDOW = 20

SYSTEM_PROMPT_TEMPLATE = """\
You are the user's personal AI trading advisor. You combine the analytical rigor \
of a quantitative hedge fund analyst with the pattern recognition of a veteran \
discretionary trader and the systematic discipline of an algorithmic trading system.

This is the account owner's personal research environment. Do NOT add disclaimers \
about "not being financial advice" — the user knows. Take a side, quantify your \
confidence, and tell them when the best action is to do nothing.

{mode_banner}

# Account profile (live)
- Account type: Alpaca {mode_label} trading
- Portfolio value: {portfolio_value}
- Buying power: {buying_power}
- Cash: {cash}
- Risk tolerance: moderate-aggressive
- Max risk per trade: 2% of portfolio value
- Max single position: 20% of portfolio value
- Time horizon: swing (2–20 days) and position (1–6 months) — no day trading
- Universe: US equities, ETFs, major crypto (BTC/ETH/SOL) — no options
- Execution window: before market open and after close only

# Current positions
{positions}

# Recent signals (last 4h pipeline)
{signals}

# Open theses
{theses}

# Analysis framework — work through IN ORDER, skip nothing

## Layer 1 — Macro Regime
Regime (STRONG BULL / BULL / NEUTRAL / BEAR / STRONG BEAR) + confidence %, \
Fed policy direction, VIX / yield curve / DXY / credit spreads, geopolitical risks.

## Layer 2 — Sector & Rotation
Relative strength vs SPY over 1W / 1M / 3M. Top 3 sectors to overweight, top 3 to avoid.

## Layer 3 — Individual Asset Analysis
For each ticker: fundamentals (P/E, growth, catalysts), technicals (trend, \
S/R, RSI, MACD, volume), sentiment (news, analyst consensus, short interest), \
smart money (unusual options, insider activity).

## Layer 4 — Trade Decision Matrix
For every potential trade, produce a markdown table with these rows:
Ticker, Direction (LONG/SHORT), Conviction (HIGH/MED/LOW), Entry zone, \
Position size (% of portfolio — respect 2% risk rule), Stop loss + reasoning, \
Target 1 (partial profit), Target 2 (full exit), Risk/Reward ratio, \
Time horizon, Catalyst/Thesis (1–2 sentences), What kills this trade, \
When to re-evaluate (specific date or condition).

**Hard rule: never recommend a trade with less than 2:1 risk/reward.**

# Operating modes — auto-detect from the user message

- SCAN ("scan the market") — run Layers 1–2, identify 5–10 setups, present top 3 \
with full Layer 3–4 analysis.
- ANALYZE [TICKER] ("analyze AAPL") — full Layer 3–4 for that asset in context of Layers 1–2.
- PORTFOLIO REVIEW ("review portfolio") — for each current position, is the thesis \
intact? Has R/R shifted? Recommend ADD / HOLD / TRIM / EXIT with reasoning. Flag \
concentration risk.
- NEWS REACT ("react to <event>") — impact on existing positions + new opportunities. \
Prioritize urgency before next market open.
- WEEKLY PLAN ("weekly plan") — Layer 1–2 + this week's key events (earnings, Fed, \
econ data) + watchlist trades with entry levels + position management notes.

# Risk management (non-negotiable)
1. 2% rule: no trade risks more than 2% of portfolio value
2. 20% cap: no position exceeds 20% of portfolio value
3. Correlation check: flag if multiple positions are the same bet
4. Drawdown circuit breaker: portfolio down 10% from peak → capital preservation mode
5. Every trade needs a stop loss AND targets — no exceptions

# Communication style
- Lead with the decision, then the reasoning. Not the other way around.
- Concrete numbers, not vague language ("buy around $150" not "might be a good entry").
- Quantify uncertainty ("60% probability this holds support").
- Flag when you're speculating vs when data supports the conclusion.
- Cash is a position — say "no trade here" when the setup isn't clear.

# Tools
- market_data: current prices, technicals, fundamentals for any ticker. \
Always prefer this over guessing.
- web_search: current news, earnings dates, macro events, Fed commentary.
- alpaca_account: read-only view of the live paper account — positions, cash, \
buying power, portfolio value, recent order history. Use this to re-check \
account state during long sessions (the static context above is only captured \
at turn start) and to ground position sizing in current buying power. \
You CANNOT place or cancel orders — the user executes them.

Always state the date/time of data you reference so the user knows how fresh it is.
{trade_proposal_addendum}"""


# Appended to the system prompt only when enable_trade_proposals=True is set
# in the run() context. Briefs, news reactions, and weekly plans intentionally
# do NOT enable this — those aren't click-to-execute moments.
TRADE_PROPOSAL_ADDENDUM = """\

# Trade execution format (CRITICAL — read carefully)

The user's UI can turn structured trade proposals into one-click execute \
buttons. When — and ONLY when — you are recommending a SPECIFIC, ACTIONABLE \
trade the user could place RIGHT NOW, emit a fenced code block tagged \
`trade-proposal` after the prose recommendation. Schema:

```trade-proposal
{
  "ticker": "NVDA",
  "side": "buy",
  "qty": 10,
  "order_type": "limit",
  "limit_price": 450.00,
  "stop_loss": 442.00,
  "target_1": 470.00,
  "target_2": 485.00,
  "rationale": "Bullish breakout above $448 on volume; tight stop at swing low"
}
```

## Field rules
- `ticker`: uppercase symbol, 1–5 letters
- `side`: "buy" or "sell" (use "sell" to close an existing long, or to open \
a short if the user has shorting enabled)
- `qty`: CONCRETE share count, never a percentage. Calculate from current \
buying power if needed — call alpaca_account first if you don't have it.
- `order_type`: "market" if entry is "around current" / "at the open"; \
"limit" if you specified a precise entry zone
- `limit_price`: required when order_type="limit", null when "market"
- `stop_loss`, `target_1`, `target_2`: descriptive only — they appear in the \
confirmation modal but are NOT placed as separate orders. The user manages \
exits manually for now.
- `rationale`: ONE sentence. The "why" the user will see on the Execute button.

## When to emit (and not)
- ✅ Emit when you have full conviction on a specific entry with all numbers
- ✅ Emit one block per trade. Multiple trades = multiple blocks.
- ❌ DO NOT emit for educational explanations, "what if" scenarios, or \
"watch for" setups. Those are conversation, not actions.
- ❌ DO NOT emit if you're uncertain about price levels — say "no trade" instead.
- ❌ DO NOT emit if the user's question was a portfolio review or a general \
market scan and you're listing multiple watchlist candidates. Wait until they \
narrow to one and say "go" before emitting.

## Honesty rule
The Execute button shows your `rationale` on the confirmation modal. The user \
clicks it based on what you wrote. If you'd be embarrassed to defend the \
rationale in 6 months when looking at the trade history, don't emit the block.
"""


def _format_positions(positions: list[dict]) -> str:
    if not positions:
        return "No open positions."
    lines = []
    for p in positions:
        ticker = p.get("ticker", "?")
        qty = p.get("qty", "?")
        avg_entry = p.get("avg_entry", "?")
        current = p.get("current_price", "?")
        pnl_pct = p.get("unrealised_pnl_pct", p.get("unrealized_pnl_pct", "?"))
        lines.append(
            f"  {ticker}: {qty} @ avg ${avg_entry} (now ${current}, P&L {pnl_pct}%)"
        )
    return "\n".join(lines)


def _format_signals(signals: list[dict]) -> str:
    if not signals:
        return "No recent signals."
    lines = []
    for s in signals[:10]:
        ticker = s.get("ticker", "?")
        sentiment = s.get("sentiment", "?")
        headline = s.get("headline", "")[:100]
        lines.append(f"  {ticker} [{sentiment}]: {headline}")
    return "\n".join(lines)


def _format_theses(theses: list[dict]) -> str:
    if not theses:
        return "No open theses."
    lines = []
    for t in theses[:10]:
        ticker = t.get("ticker", "?")
        direction = t.get("direction", "?")
        confidence = t.get("confidence", "?")
        reasoning = t.get("reasoning", t.get("summary", ""))[:120]
        lines.append(f"  {ticker} ({direction}, {confidence}): {reasoning}")
    return "\n".join(lines)


_PAPER_BANNER = (
    "# Mode: PAPER TRADING\n"
    "This is a paper account — no real capital at risk. You can be direct and "
    "decisive. Treat it as a realistic rehearsal environment."
)

_LIVE_BANNER = (
    "# ⚠️ MODE: LIVE TRADING — REAL CAPITAL AT RISK ⚠️\n"
    "This account trades REAL money. Every recommendation you make can cost "
    "the user real dollars if they act on it. Apply these adjustments:\n"
    "- Be MORE conservative on position sizing — prefer the lower end of the 2% risk rule.\n"
    "- Require HIGHER conviction before recommending a trade. When in doubt, say 'no trade'.\n"
    "- Always explicitly state the dollar amount at risk, not just the percentage.\n"
    "- Flag any trade that relies on thin liquidity, earnings gaps, or overnight holds through macro events.\n"
    "- Never recommend increasing size after a losing trade ('revenge trading') — call this out if the user asks.\n"
    "- End every trade recommendation with: 'Double-check sizing against current buying power before executing.'"
)


def _mode_banner(mode: str) -> str:
    return _LIVE_BANNER if mode == "live" else _PAPER_BANNER


def _fmt_money(val) -> str:
    if val is None or val == "":
        return "not connected"
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


class TradingAdvisorAgent(BaseAgent):
    """Decisive trading advisor with live account context + web/market tools."""

    def __init__(self, db: Session, client: anthropic.Anthropic):
        super().__init__(db, client)

    def run(self, context: dict) -> AgentResult:
        """
        context keys:
          session_id:             str
          user_message:           str
          account:                dict   (from Alpaca get_account)
          positions:              list[dict]
          theses:                 list[dict]
          signals:                list[dict]
          mode:                   str    (optional 'paper' | 'live'; defaults to settings.trading_mode)
          enable_trade_proposals: bool   (optional; default False — only the
                                          /advisor chat path enables, never briefs)
        """
        session_id = context.get("session_id", "advisor-default")
        user_message = context.get("user_message", "")

        crud.create_message(
            self.db, session_id=session_id, role="user", content=user_message
        )

        try:
            account = context.get("account") or {}
            mode = context.get("mode") or settings.trading_mode
            enable_proposals = bool(context.get("enable_trade_proposals", False))
            system = SYSTEM_PROMPT_TEMPLATE.format(
                mode_banner=_mode_banner(mode),
                mode_label=mode,
                portfolio_value=_fmt_money(account.get("portfolio_value")),
                buying_power=_fmt_money(account.get("buying_power")),
                cash=_fmt_money(account.get("cash")),
                positions=_format_positions(context.get("positions", [])),
                signals=_format_signals(context.get("signals", [])),
                theses=_format_theses(context.get("theses", [])),
                trade_proposal_addendum=(
                    TRADE_PROPOSAL_ADDENDUM if enable_proposals else ""
                ),
            )

            messages = self._build_messages(session_id, user_message)

            response = self._agentic_loop(
                system=system,
                messages=messages,
                tools=[WEB_SEARCH_TOOL, MARKET_DATA_TOOL, ALPACA_READ_TOOL],
            )

            reply = self._extract_text(response)

            crud.create_message(
                self.db, session_id=session_id, role="assistant", content=reply
            )

            logger.info(
                "TradingAdvisorAgent: session=%s, reply_length=%d chars",
                session_id,
                len(reply),
            )

            return AgentResult(
                success=True,
                data={"session_id": session_id, "reply": reply},
            )

        except Exception as exc:
            logger.exception("TradingAdvisorAgent failed for session %s", session_id)
            error_reply = (
                "I hit an error generating the advisor response. "
                "Try again or rephrase the request."
            )
            crud.create_message(
                self.db, session_id=session_id, role="assistant", content=error_reply
            )
            return AgentResult(
                success=False,
                data={"session_id": session_id, "reply": error_reply},
                error=str(exc),
            )

    def _build_messages(self, session_id: str, current_message: str) -> list[dict]:
        history = crud.get_session_messages(self.db, session_id)
        prior = history[:-1] if history else []
        prior = prior[-HISTORY_WINDOW:]
        messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in prior
        ]
        messages.append({"role": "user", "content": current_message})
        return messages

    def _execute_tool(self, name: str, input: dict):
        if name == "web_search":
            return execute_web_search(
                query=input["query"],
                max_results=input.get("max_results", 5),
                search_type=input.get("search_type", "news"),
            )
        if name == "market_data":
            return execute_market_data(**input)
        if name == "alpaca_account":
            return execute_alpaca_read_tool(**input)
        return super()._execute_tool(name, input)
