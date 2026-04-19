# User Guide

A plain-English walkthrough for using MarketCoach. If you're comfortable
with the idea of buying and selling stocks but new to professional
trading vocabulary (brackets, ATR, scale-outs, R:R), this is for you.

If you want the developer / hiring-manager view, read
[README.md](README.md) instead. If you're deploying the thing, read
[DEPLOY.md](DEPLOY.md).

---

## 1. What MarketCoach actually is

MarketCoach is an AI trading assistant. It does four things for you:

1. **Watches the market 24/7** — news, prices, macro events — without
   you having to sit in front of a screen.
2. **Emails you a plan every morning** before the US market opens. You
   read it on your phone over coffee.
3. **Proposes specific trades** with exact entry price, stop loss, and
   profit target — not just "NVDA looks good" but "buy NVDA at $540,
   stop at $526, target $567, because X."
4. **Executes trades on your real brokerage account** via Interactive
   Brokers, with safety rails that prevent the biggest mistakes
   (oversized positions, missing stop losses, accidentally going live).

It is NOT a robo-advisor or an automated "set-and-forget" system. Every
trade still requires you to click Execute yourself. The AI does the
research and math; you decide.

---

## 2. The daily loop

This is what using MarketCoach looks like, end-to-end.

**06:00 (before market open)** — a **Morning Brief** email lands in
your inbox. A 1-page markdown document: overnight moves in Asia and
Europe, US futures direction, earnings reporting today, any macro
event (Fed speaker, CPI print) that could move the tape, and
action-oriented notes on any tickers in your watchlist or positions.

**~9:00** — you read it on your phone. 90% of days there's nothing to
act on. Note any positions that need watching, close the app.

**10:30 (market open)** — if something from the brief matters, open
MarketCoach in a browser. Go to **Advisor**. Ask a specific question:
"Analyze NVDA" or "Review portfolio" or "Weekly plan."

**10:31–10:35** — advisor produces a response. If it includes a trade
proposal, a green **Execute** card appears at the bottom of its
message. Click it. A modal pops up. Fill in the required thesis box
("why am I taking this trade, in my own words") and hit submit.

**The rest of the day** — leave it alone. Automatic emails arrive when:
- A price alert you set triggers ("GLD hit $440")
- A news event happens on a ticker you care about ("NVDA earnings beat")
- A take-profit fills and the runner's stop moves to breakeven
- A scheduled check flags a meaningful change in a position

**Sunday 21:00** — a **Weekly Plan** email lands with the coming
week's key events, sector rotation, and specific trades to watch. You
plan Monday from here.

**Weekly review** — open the **Performance** page. Look at your win
rate, profit factor, and the SPY benchmark overlay. If you're not
beating SPY over the same windows, something's wrong with how you're
using the system — the advisor's job is to beat buy-and-hold, not
match it.

---

## 3. Key concepts — the vocabulary you need

Each term gets an intuitive explanation first, then why it matters for
this app.

### Bracket order / OCO

A **bracket** is three orders placed at the broker in one shot:
1. **Entry** — buy the stock at a specific limit price
2. **Take profit** — sell automatically if the stock rises to your
   target
3. **Stop loss** — sell automatically if the stock falls to your stop

The three are linked: when the take-profit OR stop-loss fires, the
other one cancels. That's what **OCO** means — "one cancels other."

**Why it matters:** without a bracket, you'd manually have to watch
the stock and place the exits yourself. Miss a day and a 5% gain can
turn into a 5% loss. The broker handling exits automatically means
you can close the laptop and the trade still manages itself.

MarketCoach puts every trade into a bracket when the advisor
recommends a valid one (entry + stop + target, stop < entry < target).
No bracket? Then the trade doesn't execute automatically — you click
Execute manually, but the exits are TODO notes for you, not orders.

### Stop loss — and why ATR-based is better than percent-based

A **stop loss** is the price at which you automatically sell to
prevent further loss. If you buy NVDA at $540 with a stop at $526,
you're saying: "if NVDA drops to $526, I accept a $14/share loss and
exit."

The naive way is **percent-based stops**: "always use a 2% stop."
That sounds simple but it's wrong for most cases:
- A 2% stop on SPY (~$11) rarely fires. You're barely risking anything.
- A 2% stop on TSLA (~$8) fires constantly on normal intraday noise.

Same 2% number, completely different meanings.

MarketCoach uses **ATR-based stops** instead. ATR stands for
Average True Range — a technical indicator that measures how much a
stock typically moves in a day. Professional rule: `stop = entry − 2 ×
ATR`. On NVDA (typical ATR ~$6), that's about a $12 stop distance. On
SPY (ATR ~$3), that's $6. The dollar amount scales automatically with
how volatile the stock is.

**You don't have to know ATR math** — the advisor calls a tool that
computes it and suggests levels. You just see the final stop price in
the trade proposal, with the reasoning ("2× ATR below entry, adjusted
down $0.50 to sit just below the 50-day moving average").

### Take profit / Target 1 / Target 2

**Take profit** is the mirror of stop loss — the price at which you
automatically sell to lock in gains. "If NVDA rises to $567, sell."

**Target 1** is the first take-profit level. In MarketCoach, this is
the one that actually gets placed at the broker.

**Target 2** is an optional second level for advanced users —
currently shown as descriptive-only in the UI (the advisor mentions
it, but no order is placed at target 2).

**R:R ratio** (reward-to-risk) is how much you stand to make vs. how
much you could lose. Target distance divided by stop distance. The app
enforces a **2:1 minimum** — if you could lose $10 on a trade, you
must be aiming to make at least $20. No exceptions.

Why 2:1: with a 50/50 win rate, 2:1 means you make money over time
because your average win is bigger than your average loss. With a 1:1
win rate you need to be right more than half the time — a much harder
bar.

### Scale-out + breakeven stop

A **scale-out bracket** is a smarter version of the standard bracket.
Instead of selling your whole position when target 1 hits, you sell
**half** at target 1 and let the other half (the **runner**) keep
running for bigger gains.

Here's the key trick: the moment target 1 fills, the runner's stop
automatically moves to your original entry price. Breakeven. From
that moment on, **you cannot lose money on the trade**. Your worst
case is: target 1 locked in profit, runner exits at breakeven for $0.

Example:
```
Buy 4 shares of SPY at $540
Stop: $534 (all 4 shares)
Target 1: $552 (sells 2 shares for +$24)

SPY rallies to $552. Target 1 fires, 2 shares sold.
Runner (2 shares) still held.
Runner's stop automatically moves from $534 → $540.

From here:
- SPY keeps rising → you continue holding, eventually sell manually
- SPY reverses back to $540 → runner sells at breakeven, you keep the
  +$24 from target 1
- SPY plummets → you're still out at breakeven on the runner, net +$24
```

**You cannot lose money on the trade once target 1 hits.** That's the
whole point.

When you click Execute on a trade proposal, there's a "Scale out at
T1 — sell half, move stop to breakeven" checkbox. It requires integer
quantity ≥ 2 (you can't split a single share). For most swing trades
with multiple shares, check it.

### Position size — how the app decides

Two hard rules the app enforces:

1. **2% rule** — no single trade can put more than 2% of your total
   portfolio at risk. With a $10,000 account and a stop $5 below
   entry, you can buy at most 40 shares ($200 risk = 2%).
2. **20% cap** — no single position can exceed 20% of your portfolio
   value at entry. With a $10,000 account, no single trade can have
   more than $2,000 committed regardless of the stop distance.

The advisor sizes positions to respect both. If its recommendation
would violate either, the modal shows the smaller qty that fits.

**Why these numbers:** the 2% rule means you can be wrong 50 times
in a row without blowing up the account — each loss is a minor dent,
not a catastrophic one. The 20% cap means no single trade can sink
your account by more than 20% in the worst case (full loss of the
position). Together they give you breathing room to be wrong a lot
while still compounding the wins.

### Thesis + invalidation

Every trade requires two things, enforced at the modal:

**Thesis** — a sentence in your own words explaining *why* you're
taking this trade. Not the advisor's reasoning — yours. "I think NVDA
will bounce off the 50-day MA because the AI narrative is still
intact." 10-character minimum. This is non-negotiable.

**Invalidation** — implicitly captured in the stop loss price. The
stop is the price at which you're saying "my thesis was wrong." If
SPY breaks below the 50-day MA by 2%, the bounce thesis is dead and
you should be out.

Why the app enforces this: thinking before trading is the single
biggest edge a retail trader has over algorithms. If you can't
articulate why you're buying, don't buy.

Later, after the trade closes, the **Journal** page will prompt you
for a **lesson** — what you learned, whether the thesis played out,
what you'd do differently. Same rule: you can't close the journal
entry until you write something.

### Paper vs live mode

MarketCoach has two trading modes:

**Paper** — trades execute against a fake account with fake money.
Same UI, same data, same broker connection, just... not real dollars.
Great for testing strategies, learning the UI, or validating that a
new feature works without risking anything.

**Live** — real money, real account, real P&L.

Switching from paper to live requires **two keys** both flipped
simultaneously:

1. `ALPACA_PAPER=false` in your `.env` file
2. `ALPACA_LIVE_CONFIRMATION=I understand this uses real capital`
   (exact string, case-sensitive, in `.env`)

If you set only one, the app refuses to start in live mode and falls
back to paper with a loud warning in the logs.

Why two keys: a single typo, a forgotten environment variable, or a
bad merge can never put real money at risk on its own. You have to
deliberately flip both, and the second one requires you to type out
a specific sentence — no muscle memory, no reflexive enter-key.

Once in live mode, every `/orders/confirm` request ALSO requires a
checkbox in the modal: "I understand this places a real-money order."
Defence in depth.

---

## 4. Using the Advisor

The Advisor is the main interactive surface. It's a Claude Opus agent
with access to live market data, news search, and a read-only view of
your brokerage account.

### Five canonical modes

The advisor auto-detects these from your message:

**`Scan the market`**
Runs a broad sweep — macro regime, sector rotation, top 5–10 setups
across your watchlist. Presents the top 3 with full trade decision
matrices.

When to use: Sunday evening, first thing Monday morning, or any time
you want "what should I be watching this week."

**`Analyze AAPL`** (any ticker)
Full Layer 3–4 analysis on one asset: fundamentals, technicals,
sentiment, smart-money signals, then a concrete trade decision matrix
if a setup exists. If no setup is clear, the advisor says "no trade
here, watch for X" instead of inventing one.

When to use: you saw a stock move, you're curious, you want a
structured opinion before committing.

**`Review portfolio`** (optionally followed by positions list)
For each current position: is the original thesis still intact? Has
the R/R shifted? Recommend ADD / HOLD / TRIM / EXIT with reasoning.
Flags concentration risk if you're too heavy in one sector.

When to use: weekly, or whenever a position has moved meaningfully.

**`React to [event]`**
Impact assessment of a specific event on your positions + new
opportunities it creates. "React to the Fed cutting rates 50bps"
would tell you which holdings benefit, which get hurt, and what new
setups the cut creates.

When to use: after major macro events — Fed days, big earnings,
geopolitical shocks.

**`Weekly plan`**
Layer 1–2 macro + sector analysis + this week's key events
(earnings, Fed speakers, economic data) + specific trades to watch
with entry levels + position management notes.

This runs automatically every Sunday at 21:00 BRT and emails you the
result. You can also trigger it manually by typing this prompt.

### Prompts that work badly

- "Is AAPL a good buy?" — too vague, the advisor will ask clarifying
  questions. Be specific: "Analyze AAPL for a swing entry this week."
- "What's the best stock to buy?" — the advisor refuses. No such
  thing as "the best"; depends on what you already hold, your risk
  tolerance, the regime.
- "Tell me what's going to happen tomorrow" — hard refusal. Nobody
  knows. The advisor will reframe into "what to watch for."

### Reading a trade proposal

When the advisor produces a tradeable setup, you'll see a structured
trade decision matrix like:

```
| Field             | Value                                        |
|-------------------|----------------------------------------------|
| Ticker            | NVDA                                         |
| Direction         | LONG                                         |
| Conviction        | MEDIUM                                       |
| Entry zone        | $540.00 (limit)                              |
| Position size     | 10 shares (~$5400, 8% of portfolio)          |
| Stop loss         | $526.40 (2× ATR-14 below entry)              |
| Target 1          | $567.20 (2:1 R:R)                            |
| Target 2          | $581.00 (3:1 R:R, scale-out runner)          |
| Risk/Reward       | 2.0:1                                        |
| Time horizon      | 5–15 days                                    |
| Catalyst/Thesis   | Bounce off 50-day MA with AI tailwind intact |
| What kills this   | Close below $525 invalidates the bounce      |
| When to re-evaluate | Weekly or if NVDA breaks $550 with volume  |
```

Underneath, a green **Execute** card. Click it to open the modal.

---

## 5. Executing a trade — full walkthrough

You clicked Execute on an NVDA proposal. A modal opens. Here's what
happens field by field.

**Confirm trade header**
> This will submit a real order to your **LIVE** broker account (IBKR).

In red if you're in live mode, green if paper. Always glance at this
before continuing.

**Action summary**
```
BUY 10 shares of NVDA
Order type: limit
Limit price: $540.00
Estimated cost: $5,400.00
```

**AUTO BRACKET panel** (blue box)
Shown when the proposal is a valid bracket. Lists the three orders
that will be placed:
```
Parent: BUY limit at $540.00
Take profit at $567.20
Stop loss at $526.40

The broker binds these as an OCO group: when one exit fills the
other cancels automatically. GTC — persists overnight.
```

**Scale-out checkbox** (inside the AUTO BRACKET panel, if qty ≥ 2)
```
☐ Scale out at T1 — sell half, move stop to breakeven
```

Check this if you want the smarter exit behavior. When checked:
```
T1 sells 5, runner holds 5 with stop auto-moved to $540.00
after T1 fills.
```

**Your thesis** (required)
```
Why are you taking this trade? (in your own words)
[10-character minimum]
```

Type something honest. "NVDA 50-day bounce setup, AI narrative
intact, sized conservatively." The advisor already gave its reasoning;
this is YOUR reasoning, in your own words. If you can't write one,
you shouldn't take the trade.

**Disagreement** (optional)
```
Do you disagree with any part of the advisor's proposal?
```

If the advisor said "stop at $526" and you want $528 instead, write
"tightening stop to $528 to sit above yesterday's low, slightly
worse R:R but cleaner invalidation." Gets captured for future
journal review.

**Live confirmation checkbox** (live mode only)
```
☐ I understand this places a real-money order using my live broker
account. I have verified the ticker, side, quantity, and price above.
```

Required before Submit enables. Glance at the numbers one more time.

**Submit**
The request hits `/orders/confirm` on the backend. Seven safety gates
run in order:
1. Pydantic schema validation (obvious malformed requests rejected)
2. Bracket consistency (`stop < entry < target`)
3. Live-mode confirmation (if applicable)
4. Broker must be connected
5. Daily order cap (max 3 orders per 24h)
6. Ticker whitelist (must be in positions, watchlist, or open theses)
7. 20% position cap (notional ≤ 20% of portfolio)

If any gate rejects, you see a specific error message explaining
why. If all pass, the orders submit to IBKR and a journal entry
opens with your thesis.

The modal closes. You're done.

---

## 6. Going live — the safety ritual

Step-by-step the first time you activate real-money trading.

### Prerequisites

- IBKR live account approved and **funded with real money**
- IB Gateway installed (Java desktop app from IBKR's site)
- You understand that real losses are possible

### Step 1 — IB Gateway logged into live

Open IB Gateway. On the login screen, select **"Live Trading"** (not
"Paper Trading"). Log in with your IBKR credentials. Complete 2FA on
your phone. Confirm Gateway is listening on port **4001** for live
(4002 is paper).

If Gateway refuses to connect to live (common for new accounts), fix
that first before touching MarketCoach. The app can't help if the
broker connection isn't working.

### Step 2 — Edit `.env`

Open `marketcoach/.env`. Make three changes:

```
IBKR_PORT=4001           # was 4002
ALPACA_PAPER=false
ALPACA_LIVE_CONFIRMATION=I understand this uses real capital
```

**You type the confirmation phrase yourself.** No copy-paste (well,
you can, but pay attention). The exact string is case-sensitive.

Save the file.

### Step 3 — Restart the backend

Kill the running uvicorn (Ctrl+C in its terminal), restart:
```
cd marketcoach
py -3.12 -m uvicorn backend.main:app --reload
```

In the log, look for this banner:
```
═══════════════════════════════════════════════════════════
  BROKER (IBKR) INITIALISED IN LIVE MODE
  Real capital is at risk.
═══════════════════════════════════════════════════════════
```

If you see that — live mode is active.

If you see `Broker (ibkr) initialised in paper mode` — the
confirmation phrase is wrong. Re-check `.env` for typos, save,
restart.

If you see `ALPACA_PAPER=false but ALPACA_LIVE_CONFIRMATION is
missing or incorrect` — same fix.

### Step 4 — Verify the balance

Open http://localhost:5173/portfolio. The equity number **must match
what IB Gateway's own account summary shows**. If it shows paper
account balance (typically $1M) or $0, something routed wrong. Stop.

### Step 5 — First live trade

Not a real signal — a plumbing test. **1 share of a liquid name**
(SPY, QQQ, or AAPL), full bracket. Submit via the Advisor Execute
card. Watch the backend log for:
```
IBKR bracket submitted: BUY SPY x1.00 @ $... → Submitted (paper=False, fill=None)
```

Open IB Gateway's Orders tab. Confirm three orders appear: parent
buy + take-profit + stop-loss, all sharing a parent ID.

If that all works, the full path is verified. Your next trades can
be normal sized.

### If something goes wrong

- **Back out immediately** by reverting `.env` to paper and
  restarting. Paper mode is always safe.
- **Don't place a second trade** while diagnosing the first. Two
  ambiguous things happening at once is how mistakes compound.
- **Check the trade journal.** Even rejected orders get a row with
  the rejection reason.

---

## 7. Reading the Performance dashboard

The `/performance` page is where you find out whether any of this
actually works — whether the AI's proposals plus your judgment beat
just holding the market.

### Hero cards (top row)

**Win Rate** — % of closed trades that were profitable. 50%+ is fine
if your R:R is > 1:1. 60%+ is strong. Below 40% means something's
wrong.

**Realised P&L** — total dollars made or lost across all closed
trades. Simple aggregate. Color-coded.

**Profit Factor** — total gross wins divided by total gross losses.
> 2.0 = Excellent. The app labels it so you don't have to remember.
> 1.0 = Profitable but not by much.
< 1.0 = Losing money overall.

**Avg Hold** — average days held per trade. Tells you whether you're
actually swing-trading (2–20 days) or accidentally day-trading (< 1
day) or stuck holding turkeys (> 30 days).

### Secondary cards

**Largest Win / Largest Loss** — are you letting winners run AND
cutting losers short? If your largest win is much bigger than your
largest loss, you're doing it right. If they're similar, you're
exiting winners too early or holding losers too long.

**Avg Win / Avg Loss ratio** — should be ≥ 1.0 at minimum. 1.5+ is
good. Tells you whether your winners are meaningfully bigger than
your losers on average.

### The equity curve chart

Cumulative realised P&L over time. Each step = one closed trade.
Line going up = making money. Going down = losing.

**SPY benchmark overlay** (grey dashed line) — this is the important
one. It shows what you would have made by buying SPY with the same
dollars, on the same days, for the same holding periods. If your
green line is ABOVE the grey line, your active trading is adding
value. If it's below, you'd be better off just holding SPY.

**Alpha pill** (top right of the chart) — the numerical difference
between your realized P&L and the SPY benchmark. Positive = beating
the market. Negative = underperforming. The grey zone (±$0.50) is
statistical noise at small sample sizes.

### Advisor attribution card

Shows win rate of trades that came from advisor chat (have an
advisor_session_id) vs. your overall win rate. ±points delta tells
you whether advisor-sourced trades do better or worse than your gut
trades.

If advised trades win significantly less than your gut trades:
you're ignoring the advisor's bad proposals correctly (good) but
following its mediocre ones (bad). Filter harder.

If advised trades win significantly more: follow the advisor more
often. Your gut is off.

### Recent closed trades table

Last 50 closed trades. Columns: ticker, qty, entry, exit, P&L ($ and
%), days held, close date, source (advisor or manual). Click an
advisor-sourced row to find the original conversation.

### What to actually DO with this data

- **< 10 closed trades:** sample too small to mean anything. Keep
  trading, keep journaling, revisit at 30 trades.
- **30+ closed trades:** patterns become real. If Profit Factor is
  below 1.0 after 30 trades, your process is net losing. Don't trade
  live until you understand why — usually either trades are too big
  (bigger than 2% risk), or stops are too tight (getting shaken out),
  or you're overriding the advisor in the wrong direction.
- **100+ closed trades:** the data is statistically meaningful. If
  you're beating SPY consistently, the system works for you. If
  you're not, either change the process or accept that buy-and-hold
  is better for you.

Don't obsess over small changes over 5 trades. The data you need to
answer "is this working?" takes months to accumulate.

---

## 8. Troubleshooting — common errors

### "Bracket orders require BOTH stop_loss and target_1. Set both, or neither."

The advisor gave an incomplete proposal (stop but no target, or
vice versa). Either wait for a more complete proposal, or manually
set the missing level in the modal before submitting.

### "Bracket levels inconsistent: need stop_loss < limit_price < target_1"

The stop is above the entry, or the target is below. Shouldn't
happen on long trades — check that the levels make sense. Buy
trades: stop below entry, target above.

### "Broker not configured — cannot cancel orders."

Your local backend isn't reachable or broker singleton isn't
initialised. Check:
1. Is the backend running? (`uvicorn` in a terminal somewhere?)
2. Is `BROKER_PROVIDER=ibkr` in `.env`?
3. Is IB Gateway running and logged in?

On Railway, `BROKER_PROVIDER=none` is intentional — trading is
disabled on the cloud service. Do trading from your local backend.

### "Daily order cap reached (3/3)"

You've placed 3 orders in the last 24 hours. This is a safety limit
that protects against runaway situations. Either:
- Wait until the rolling 24h window moves forward
- Bump `ORDER_DAILY_CAP` env var temporarily if you have a real
  reason (a basket of new positions on Monday morning)

### "Order notional $X exceeds 20% of portfolio ($Y)"

Position size too big. Reduce qty until the notional fits under
20% of your portfolio value. This cap is load-bearing for the whole
risk framework — don't bump it casually.

### "System is in live mode. Set confirm_live_capital=true in the request body."

You're in live mode but didn't tick the confirmation checkbox in
the modal. Tick it and resubmit.

### "Live trading requires explicit user confirmation"

Pre-removal message from a broker safety check. If you see this
after the Alpaca removal, something unusual happened — restart the
backend and try again.

### "Ticker not in positions, open theses, or watchlist"

The app refuses to trade tickers you haven't expressed interest in
through one of those three channels. Prevents the advisor from
randomly trading a ticker you've never heard of. Fix: add it to your
watchlist on the Portfolio page first.

### "Advisor call failed: credit balance is too low"

Your Anthropic API key is out of funds. Top up at
console.anthropic.com/settings/billing. Takes a few minutes to
propagate.

### Email notifications not arriving

1. Check `NOTIFICATIONS_ENABLED=true` in the environment where the
   scheduler runs (Railway, for morning brief / weekly plan).
2. Check `SMTP_USER` + `SMTP_PASSWORD` are set. Gmail requires an
   **App Password** (16 chars, from myaccount.google.com/apppasswords),
   not your regular Gmail password.
3. Check the spam folder. First email from a new sender sometimes
   lands there.
4. Check Railway logs for `Email send failed` or `SMTP auth failed`.

---

## 9. Safety features — what protects you from yourself

Every trade goes through seven gates on the backend. Each exists
because of a real risk. You don't normally see them unless one
rejects your order.

**Gate 1 — Schema validation.** Catches obvious malformed requests
(negative quantity, invalid ticker format, missing required fields).
Returns HTTP 422 with a specific reason.

**Gate 2 — Bracket consistency.** If you send a bracket order, the
levels must make sense: `stop < entry < target` for a buy, both
stop_loss and target_1 must be set together (not one without the
other). Returns 422 otherwise.

**Gate 3 — Live-mode confirmation.** When the backend is in live
mode (`settings.is_live_mode` = True), every single request must
carry `confirm_live_capital=true` in the body. Paper mode doesn't
need it. Returns 403 if missing.

**Gate 4 — Broker connection.** If `get_broker()` returns None (no
broker configured, Gateway not running, etc.), returns 503 with
an actionable error. No silent failures.

**Gate 5 — Daily order cap.** Max 3 orders per 24h window by default.
Stops a runaway loop from placing 50 orders. Returns a soft reject
(logged as `rejected` in the executed_orders table).

**Gate 6 — Ticker whitelist.** Must be in positions, open theses, or
watchlist. Prevents the advisor from randomly proposing tickers
you've never expressed interest in. Two exceptions:
- Trades with `advisor_session_id` + `rationale` auto-whitelist the
  ticker (you intentionally asked the advisor about it)
- Dual-signal check: both must be set, not just one — prevents
  arbitrary API calls from bypassing this gate

**Gate 7 — 20% position cap.** At entry, no single position can
exceed 20% of your total portfolio value. Enforced at the notional
level (`qty × entry_price`). The 2% risk rule is advisory (the
advisor sizes positions to respect it); this 20% notional cap is
the hard ceiling.

After all seven pass, the order is submitted to the broker. Then:

**Post-submit — atomic bracket.** When the broker is IBKR, the
bracket is placed as a single OCA group — all three orders share a
parent ID, and when one exit fills the broker automatically cancels
the others. No polling loop on our side, no window where a fill
leaves the position unprotected.

**Post-submit — trade journal.** Every submitted order (and every
rejection) creates a row in the trade journal with your thesis, the
advisor's rationale (if present), and the submission timestamp.
Immutable audit trail.

These gates exist because trading real money is one of the few
software domains where a silent bug can genuinely cost you. The app
errs on the side of refusing ambiguous requests with a clear
message. If a gate rejects something you wanted to do, read the
error — usually you'll realize it's correct.

---

## That's it

Three things that are worth re-reading after you've used the app for
a week:

- **Section 2** (daily loop) — once the rhythm is natural you'll
  stop thinking about it. Until then, refer back.
- **Section 3** (key concepts) — the trading vocabulary compounds.
  Knowing what a scale-out does under the hood helps you decide when
  to check the box.
- **Section 7** (performance dashboard) — how you'll measure whether
  any of this is actually helping you make money. Come back here
  every month with a clean head.

Questions or issues not covered here: open an issue on the GitHub
repo or check the inline code comments — the codebase documents
itself pretty thoroughly, especially the
[`backend/main.py`](marketcoach/backend/main.py) endpoints and the
[`backend/agents/`](marketcoach/backend/agents/) pipelines.
