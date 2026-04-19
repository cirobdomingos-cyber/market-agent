# MarketCoach

**AI-powered personal trading platform.** LLM-driven market analysis +
live execution via Interactive Brokers, running 24/7 on Railway with a
local broker gateway. Built for swing trading: weekly plan on Sunday,
morning brief on weekdays, bracket orders with automatic breakeven-stop
on partial fills, and a feedback loop that measures whether the AI's
recommendations actually perform.

Status: **in live production use** against a real IBKR account (paper
& live). **400+ tests passing**. Cost-optimized to ~$20–30/month total
infrastructure + API spend for a swing-trading cadence.

---

## What it does

Three loops, running at different cadences.

**1. Intelligence (folded into the weekly plan on swing-trader mode)**

A scheduled pipeline fetches financial news, extracts structured
signals via Claude Haiku, generates 3–5 theses via Claude Sonnet, and
closes the loop by resolving expired theses against real price
movement (yfinance). Prediction accuracy is tracked per thesis and
aggregated into a measurable win rate.

**2. Advisor (interactive + on-demand)**

A Claude-Opus agent with tool use (`market_data`, `web_search`,
`broker_read`) that produces structured trade proposals. Every
proposal carries entry, stop, target, position size, R:R, invalidation
conditions, and re-evaluation trigger. The advisor's system prompt is
cache-split: a stable ~4k-token framework prefix (cached) + a dynamic
account-snapshot suffix. Repeat calls within the 5-minute cache window
pay ~10% of input cost on the stable block.

Morning brief (weekdays 06:00 BRT) and weekly plan (Sunday 21:00 BRT)
invoke the same advisor at scheduled times and email the output. A
state-aware gate on news reactions suppresses advisor calls when the
prior analysis is still fresh (< 3% price move AND < 48h elapsed) —
cuts ~40–60% of redundant token spend during normal operation.

**3. Execution (real money, with guardrails)**

Clicking Execute on a trade proposal runs it through seven gates:
Pydantic schema → bracket consistency → live-mode confirmation →
broker connected → daily cap → ticker whitelist → 20%-per-position
rule → atomic order submission → DB persistence + trade journal.

Bracket orders are placed as **OCO groups at IBKR**: parent entry +
take-profit + stop-loss, all bound at the broker so when one exit
fires the others cancel automatically. Scale-out brackets take this
further — take-profit sells half, and when it fills the runner's stop
automatically moves to the entry price (breakeven). From that point
the trade cannot lose money.

---

## Screenshots

_Screenshots not yet captured. Pages worth showing when they are:_
_Markets (live prices + clickable chart), Advisor (full conversation
with a trade-proposal card), Performance (KPI cards + equity curve),
Morning Brief email on phone._

---

## Architecture

```
                          EMAIL (Gmail SMTP)
                                 ^
                                 | morning brief, weekly plan,
                                 | news reactions, price alerts
                                 |
┌────────────────────────────────┴──────────────────────────────────┐
│ Railway (24/7 scheduler + analysis)                                │
│                                                                     │
│   APScheduler → Orchestrator                                        │
│     ├─ Intelligence pipeline (news → theses → trade ideas)          │
│     ├─ Morning brief + weekly plan (TradingAdvisor agent)           │
│     ├─ State-aware advisor gate (material-change rule)              │
│     ├─ Position poll (5–30 min) → scale-out state machine           │
│     └─ Price alerts (atomic mark-as-triggered)                      │
│                                                                     │
│   FastAPI endpoints (40+)                                           │
│     └─ /performance, /orders/confirm, /advisor, /market-data, …     │
│                                                                     │
│   SQLite on mounted volume (/data/marketcoach.db)                   │
│   Lightweight additive migrations on startup (no Alembic)           │
└─────────────────────────────────────────────────────────────────────┘
                                 ^
                                 | HTTPS via Vite proxy,
                                 | bearer auth from .env.local
                                 |
┌────────────────────────────────┴──────────────────────────────────┐
│ Local laptop (when you're trading)                                  │
│                                                                     │
│   React 18 + Vite + Tailwind + Recharts                             │
│     Dashboard · Markets · Advisor · Portfolio · Performance         │
│     Trade Ideas · Journal · Executed Orders · Morning Brief ·       │
│     Weekly Plan · News · Coach · Calendar · Backtest                │
│                                                                     │
│                                 |                                   │
│                                 v                                   │
│   Local FastAPI (for trading ops that need IB Gateway)              │
│                                 |                                   │
│                                 v                                   │
│   IB Gateway (Java GUI, TCP socket)                                 │
│     ib_insync on a dedicated daemon-thread event loop               │
│     Paper port 4002 / 7497 · Live port 4001 / 7496                  │
└─────────────────────────────────────────────────────────────────────┘
```

**Why split architecture:** IB Gateway is a local-only Java GUI app;
IBKR's live accounts enforce 2FA on every login, which makes headless
cloud deployment impractical. Cloud-hosted backend runs everything
that doesn't need the broker (analysis, scheduled jobs, notifications)
and returns 503 cleanly from any endpoint that does. Frontend talks
to whichever backend its `VITE_API_URL` points at.

Full runbook in [DEPLOY.md](DEPLOY.md).

---

## Engineering highlights

Worth reading if you're reviewing the repo as an engineering signal
rather than a trading tool. Each one links to the commit that shipped
the pattern.

**Per-agent model tiering**
[`backend/agents/base.py`](marketcoach/backend/agents/base.py) — each
agent subclass can override `MODEL`. News extraction uses Haiku (~10x
cheaper, parity quality on structured tasks); reasoning agents
(advisor, analysis, trade ideas, coach, memory) use Sonnet or Opus. A
regression test locks Sonnet on the reasoning agents so the override
pattern can't silently flip back.

**State-aware LLM call gating**
[`backend/agents/orchestrator.py`](marketcoach/backend/agents/orchestrator.py) —
before firing an advisor call on a news signal, checks a per-ticker
cache: if price has moved < 3% AND last run was < 48h ago, skip and
write a lightweight "suppressed" row. Biggest single win for API cost
under swing-trading cadence. The value is in not asking the question,
not in asking it more cheaply.

**OCA bracket construction for scale-out**
[`backend/brokers/ibkr.py`](marketcoach/backend/brokers/ibkr.py) —
scale-out brackets can't use `ib.bracketOrder()` (forces equal
quantities on all legs). Instead: parent LIMIT + OCA-grouped
(take-profit + stop_a) on the T1 qty + standalone stop_b on the
runner. When T1 fills, OCA auto-cancels stop_a; a separate state
machine (runs every poll) detects the qty delta and modifies stop_b's
trigger to the entry price. True in-place modify via `placeOrder` with
the same orderId — no cancel-and-replace window where the runner is
unprotected.

**IBKR thread affinity**
[`backend/brokers/ibkr.py:80–135`](marketcoach/backend/brokers/ibkr.py) —
ib_insync is single-threaded and binds to the event loop that called
`ib.connect()`. FastAPI's anyio threadpool hands each request to a
different worker, which breaks the second call. Fix: a dedicated
daemon thread owns the event loop; every IB op submits a coroutine via
`asyncio.run_coroutine_threadsafe`. Canonical pattern for integrating
blocking sync code with a single-threaded async library.

**TOCTOU-safe price-alert firing**
[`backend/db/crud.py`](marketcoach/backend/db/crud.py) —
`mark_alert_triggered` uses a conditional `UPDATE ... SET active=False
WHERE id=? AND active=True`, returns True only if rowcount=1. Two
overlapping poll workers can both observe an alert as active, but only
one wins the atomic update and fires the downstream notification.
Plus create-time dedupe on `(ticker, condition, target_price)` that
collapses accidental double-clicks. Fixed a bug where a single alert
produced 3 identical emails.

**Two-key live-mode safety gate**
[`backend/config.py`](marketcoach/backend/config.py) — live trading
requires BOTH `ALPACA_PAPER=false` AND
`ALPACA_LIVE_CONFIRMATION="I understand this uses real capital"` (the
exact string). Either flag alone falls back to paper with a loud
warning. Defence in depth — a single typo or accidental merge cannot
put real money at risk.

**Hermetic test fixtures against the developer's .env**
[`tests/conftest.py`](marketcoach/tests/conftest.py) — an autouse
fixture forces `api_secret=""`, `alpaca_paper=True`,
`alpaca_live_confirmation=""`, `notifications_enabled=False` for every
test. Without these, a developer with `API_SECRET` set locally got 401
on 103 tests; with `NOTIFICATIONS_ENABLED=true`, the suite would send
real SMTP emails on every pytest run. Test suite must be hermetic to
the operator's local configuration.

**Lightweight additive migrations**
[`backend/db/__init__.py`](marketcoach/backend/db/__init__.py) —
`_apply_additive_migrations` runs on every `init_db()`. Adds missing
columns to existing tables via `ALTER TABLE ... ADD COLUMN`. Additive
only — never drops, renames, or changes types. Long-lived databases
(developer machines, Railway's mounted volume) pick up new columns
automatically without Alembic ceremony. Solved a real production
incident where a new column broke startup on any DB created before it.

**Prompt caching on the advisor system prompt**
[`backend/agents/trading_advisor_agent.py`](marketcoach/backend/agents/trading_advisor_agent.py) —
system prompt is split into two content blocks: a stable prefix
(framework + role + tools) marked `cache_control: ephemeral`, and a
dynamic suffix (account snapshot, positions, open theses) re-rendered
each call. Repeat calls within 5 minutes pay ~10% of input cost on the
stable block. Most valuable during batched news-reaction runs where
multiple advisor calls fire in close succession.

**Timezone correctness across environments**
[`backend/scheduler.py`](marketcoach/backend/scheduler.py) —
`SCHEDULER_TIMEZONE` setting passed to every `CronTrigger`, resolved
via `zoneinfo.ZoneInfo` (stdlib, no pytz). Without it, Railway runs
in UTC, and the morning brief at `hour=6` fires at 03:00 BRT instead
of 06:00. Invalid IANA names log a warning and fall back to server
local; empty preserves pre-feature behaviour.

---

## Tech stack

| Layer | Tech | Notes |
|---|---|---|
| Backend framework | Python 3.12, FastAPI, Uvicorn | Async-first API, bearer auth |
| Data | SQLAlchemy 2.0, SQLite | Lightweight additive migrations; Postgres-ready |
| Config | pydantic-settings | `extra="ignore"` for staged rollout |
| Scheduling | APScheduler (BackgroundScheduler) | Cron + interval triggers with IANA timezone |
| LLM | Anthropic SDK — Haiku / Sonnet / Opus | Per-agent model tier, prompt caching, tool-use agentic loop |
| Market data | yfinance | Quotes, history, technicals, fundamentals |
| Broker | Interactive Brokers via ib_insync | Single-loop daemon thread for thread-affinity safety |
| Frontend | React 18, Vite, Tailwind, Recharts, react-markdown, react-router | Dark-mode-first, responsive |
| Notifications | Gmail SMTP with App Password | Fire-and-forget — never blocks the scheduler |
| Testing | pytest, FastAPI TestClient, StaticPool in-memory SQLite | 400+ tests, ~35s suite, zero external deps |
| CI | GitHub Actions | Parallel backend pytest + frontend vite build |
| Deploy | Railway (backend 24/7) + local IB Gateway | Split architecture; see DEPLOY.md |

---

## Getting started

### Prerequisites

- Python 3.12+
- Node.js 20+
- [Anthropic API key](https://console.anthropic.com)
- (For live trading) Interactive Brokers account with IB Gateway
  installed locally

### Backend

```bash
cd marketcoach
cp .env.example .env
# Edit .env: ANTHROPIC_API_KEY is required

pip install -r requirements.txt
python -m uvicorn backend.main:app --reload
```

API on `http://localhost:8000`, interactive docs at `/docs`.

### Frontend

```bash
cd marketcoach/frontend
npm install
npm run dev
```

UI on `http://localhost:5173`. The dev server proxies `/api/*` to
`localhost:8000` by default; set `VITE_API_URL` in
`marketcoach/frontend/.env.local` to point at a remote backend (e.g.
Railway) instead.

### Run the tests

```bash
cd marketcoach
python -m pytest tests/ -q
```

~35 seconds, zero external dependencies (Anthropic, IBKR, yfinance,
SMTP are all mocked).

### Go live (optional, real money)

Read [DEPLOY.md](DEPLOY.md) first. The two-key safety gate is
deliberately inconvenient to flip; don't shortcut it.

---

## Cost footprint

For a swing-trading cadence (weekly plan + daily morning brief +
occasional manual advisor use):

- **Anthropic:** ~$15–30/month. News extraction on Haiku, reasoning
  agents on Sonnet, advisor on Opus only when decisions actually need
  making. State-aware gating + 48h dedupe window keeps it capped.
- **Railway:** ~$5/month for the backend service + volume.
- **Gmail SMTP:** free (App Password).
- **yfinance:** free, unlimited.
- **IBKR commissions:** pass-through, not infrastructure.

Total infrastructure: **~$20–35/month** for a 24/7 automated trading
intelligence system with real-broker integration.

---

## Project structure

```
market-ai-agent/
├── marketcoach/
│   ├── backend/
│   │   ├── main.py                  # FastAPI app, 40+ endpoints
│   │   ├── config.py                # pydantic-settings
│   │   ├── scheduler.py             # APScheduler jobs + timezone
│   │   ├── notifications.py         # Gmail SMTP
│   │   ├── agents/
│   │   │   ├── orchestrator.py      # Pipeline composition + state machines
│   │   │   ├── trading_advisor_agent.py
│   │   │   ├── news_agent.py        # Haiku
│   │   │   ├── analysis_agent.py
│   │   │   ├── trade_idea_agent.py
│   │   │   ├── coach_agent.py
│   │   │   ├── memory_agent.py
│   │   │   └── base.py              # BaseAgent + agentic loop
│   │   ├── brokers/
│   │   │   ├── base.py              # BrokerClient ABC
│   │   │   ├── ibkr.py              # ib_insync + threading
│   │   │   └── factory.py           # Provider selection
│   │   ├── db/
│   │   │   ├── models.py            # SQLAlchemy ORM
│   │   │   ├── crud.py              # Every query, one file
│   │   │   └── __init__.py          # init_db + _apply_additive_migrations
│   │   ├── tools/                   # Tools Claude agents can call
│   │   │   ├── market_data.py       # yfinance
│   │   │   ├── web_search.py        # DuckDuckGo
│   │   │   └── broker_tool.py       # Read-only broker access for agents
│   │   └── backtest/                # Time-machine simulation
│   ├── frontend/
│   │   └── src/
│   │       ├── pages/               # 13 pages
│   │       ├── components/          # ConfirmTradeModal, SellModal, EquityChart, ...
│   │       └── App.jsx
│   ├── tests/                       # 400+ tests, split by concern
│   └── requirements.txt
├── .github/workflows/tests.yml      # CI
├── DEPLOY.md                        # Railway runbook
└── README.md                        # you are here
```

---

## Known limitations & what's next

**Deliberately not built:**
- **Options trading.** Cash equities + ETFs only, no derivatives.
- **Short selling.** Bracket v1 is long-only; shorts need reversed
  stop/target semantics and the complication isn't worth it for a
  swing-trader account.
- **Fractional-share scale-out.** Scale-out brackets require integer
  qty ≥ 2 because exchanges don't split sell orders across prices at
  the fractional level.

**Next features on the roadmap:**
- Benchmark vs SPY on the performance equity curve
- Mobile-responsive tweaks so the phone-email → act-on-phone loop closes
- ATR-based stop suggestions in the advisor's trade decision matrix

---

## License

[MIT](LICENSE)
