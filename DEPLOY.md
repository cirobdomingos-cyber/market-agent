# MarketCoach deploy runbook

Split-architecture deploy: **backend on Railway (24/7 analysis), IB Gateway at home (trading)**, frontend runs locally and points at whichever backend the user wants.

This is Phase 1. Phase 2 (self-hosted IB Gateway + full cloud trading) is out of scope here.

---

## Why split the architecture

MarketCoach needs three things running:

1. **Scheduled analysis jobs** — news pipeline, morning brief, weekly plan. These want to run 24/7 regardless of whether a laptop is on.
2. **Interactive UI** — the advisor chat, portfolio, journal. Only used when the user is actively working.
3. **Broker access** — IB Gateway, a Java GUI desktop app that cannot be cleanly run on most PaaS providers and is subject to IBKR's TOS constraints around cloud hosting for live accounts.

The split:

| Layer | Where | Always on? |
|---|---|---|
| FastAPI backend + scheduler + SQLite | **Railway** | ✓ |
| Frontend (Vite dev server) | **Local laptop** | when user works |
| IB Gateway | **Local laptop** | when user trades |

The broker abstraction handles the "broker unreachable" case on Railway by using `BROKER_PROVIDER=none`. Every broker-dependent call site short-circuits cleanly; analysis features work, trading endpoints return 503 with actionable errors.

---

## Prerequisites

- Railway account + a new project created from this GitHub repo
- An Anthropic API key
- A generated API secret (any strong random string — used for bearer auth once deployed)
- `.env` on the local laptop still configured for paper IBKR (IBKR_PORT=4002, `ALPACA_PAPER` unset or `true`)

---

## Environment variables

### Complete inventory

See [`marketcoach/.env.example`](marketcoach/.env.example) for the full list with inline comments. The backend reads everything through a single `pydantic_settings.Settings` class in [`marketcoach/backend/config.py`](marketcoach/backend/config.py) — there are no `os.environ` calls anywhere else in the codebase.

### Classification

| Category | Must-set on Railway | Keep at defaults | Never set on Railway |
|---|---|---|---|
| Secrets | `ANTHROPIC_API_KEY`, `API_SECRET` | — | `ALPACA_*` (Alpaca not used), `IBKR_*` (Gateway is local) |
| Infra | `DATABASE_URL`, `SCHEDULER_TIMEZONE`, `BROKER_PROVIDER=none` | `CORS_ORIGINS` | — |
| Safety gate | `ALPACA_PAPER=true` (explicit) | `ALPACA_LIVE_CONFIRMATION` (must stay empty) | — |
| Behaviour | — | everything `NEWS_*`, `WEEKLY_*`, `MORNING_*` | — |

### Railway paste block

```bash
# secrets
ANTHROPIC_API_KEY=sk-ant-...
API_SECRET=<generate strong random string>

# infra
DATABASE_URL=sqlite:////data/marketcoach.db
SCHEDULER_TIMEZONE=America/Sao_Paulo

# broker — explicit no-op so the backend doesn't try to reach IB Gateway
BROKER_PROVIDER=none

# safety gate — explicit paper, belt and suspenders
ALPACA_PAPER=true
```

`SCHEDULER_TIMEZONE` is a new setting added in Stage D (see below) — without it, APScheduler uses the server's local timezone (UTC on Railway) and the morning brief would fire at 06:00 UTC = 03:00 BRT.

---

## Stage plan

Each stage is a separate commit on the `feat/railway-deploy` branch so a single stage can be reverted cleanly if something breaks.

### Stage A — Broker-degradation audit + `BROKER_PROVIDER=none` ✅ done (commit `da52879`)

Confirmed every `get_broker()` call site handles `None` gracefully. Added a `"none"` factory option so the startup code can explicitly opt out of broker construction instead of triggering 10-15s TCP-connect timeouts per call. Also cleaned up three leftover "Alpaca"-branded strings in broker-neutral responses.

Files: `backend/brokers/factory.py`, `backend/main.py`, `tests/test_brokers.py`. Tests: 310 → 313 passing.

### Stage B — Config + secrets inventory ✅ done (this commit)

Audited all 26 env vars. Updated [`marketcoach/.env.example`](marketcoach/.env.example) to cover every setting (was missing IBKR vars, safety gate, news reactions, position reviews, weekly plan cron, morning brief cron). Wrote this `DEPLOY.md`.

Verified: `.env` is gitignored, was never tracked in git history, no secrets leaked anywhere in the repo.

### Stage C — SQLite persistence via Railway volume (next)

- Attach a Railway volume to the backend service at `/data` (~$0.25/GB/month)
- Upload the existing local `marketcoach.db` into the volume on first deploy (preserves journal, executed orders, equity snapshots, watchlist, price alerts)
- Verify `DATABASE_URL=sqlite:////data/marketcoach.db` resolves to the mounted volume
- APScheduler is single-process in-memory so multi-replica is not supported anyway — the single-replica volume constraint is fine

No code change required. Pure infra.

### Stage D — First deploy + scheduler timezone fix

**Code change:** add a `scheduler_timezone: str = ""` setting. In `scheduler.py`, pass `timezone=pytz.timezone(settings.scheduler_timezone)` to every `CronTrigger` when the setting is non-empty.

**Railway setup:**
1. New service from GitHub repo
2. Root directory: `marketcoach`
3. Start command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
4. Attach volume (from Stage C)
5. Set env vars (from the paste block above)
6. Deploy

**Success criteria in Railway logs:**
- `MarketCoach starting up`
- `Broker provider='none' — broker features disabled.`
- `Scheduler started — intelligence=4h, weekly=sun 21:00, morning=mon-fri 06:00, position_poll=disabled`
- `Application startup complete.`
- Health endpoint at `https://<railway-domain>/health` returns `{"status": "ok", "broker_connected": false}`

### Stage E — Point local frontend at Railway

**Code change:** [`marketcoach/frontend/vite.config.js`](marketcoach/frontend/vite.config.js) reads `VITE_API_URL` via Vite's `loadEnv` and uses it as the proxy target, falling back to `http://localhost:8000`.

**Local setup:**
1. Create `marketcoach/frontend/.env.local` with `VITE_API_URL=https://<railway-domain>`
2. Restart Vite dev server
3. Load http://localhost:5173
4. Expect: analysis features work, `/portfolio` shows "Broker not configured" (correct — Railway has no broker), trading endpoints return 503

**For trading:** switch `VITE_API_URL` to `http://localhost:8000` (or comment it out), restart Vite, restart the local backend, start IB Gateway. Local backend has the broker; Railway backend has the 24/7 scheduler. Two deployment targets of the same codebase is unusual but legitimate — it's the simplest way to keep analysis always-on without exposing the home network.

---

## What does NOT work on Railway (by design)

These endpoints return 503 or empty responses when `BROKER_PROVIDER=none`:

- `GET /portfolio` → `{account: {status: disconnected}, positions: []}`
- `GET /portfolio/orders` → `[]`
- `POST /orders/confirm` → HTTP 503 "Broker not configured"
- `GET /health` → `{broker_connected: false}` (but `status: ok`)
- Advisor calls to `BROKER_READ_TOOL` → returns an error dict, advisor falls back to text recommendation
- Position polling job → `if broker is None: return 0` no-op

Analysis features that keep working:

- News pipeline + signals + theses
- Morning brief, weekly plan
- Trade ideas, news reactions
- Equity history (reads DB, doesn't poll broker)
- Journal, accuracy stats, backtest
- Advisor chat (falls back to general recommendations when broker tool unavailable)

---

## Known issues to track

- **Timezone:** requires `SCHEDULER_TIMEZONE` env var + a code change in `scheduler.py` (Stage D)
- **Equity history discontinuity:** if live mode is later activated, `equity_snapshots` will show a cliff where the paper-account history ends and live-account snapshots begin. Not a deploy blocker. Future fix: partition snapshots by `is_paper`.
- **Bug #1 from HANDOFF.md:** fixed in commit `8affa28` (`place_order` now correctly routes limit orders through `LimitOrderRequest` / `_LimitOrder` instead of silently downgrading to market).
- **Alpaca removal refactor:** queued for a separate branch. Not required for Phase 1 deploy.
