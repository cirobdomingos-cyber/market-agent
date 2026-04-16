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

### Stage B — Config + secrets inventory ✅ done (commit `cae5f25`)

Audited all 26 env vars. Updated [`marketcoach/.env.example`](marketcoach/.env.example) to cover every setting (was missing IBKR vars, safety gate, news reactions, position reviews, weekly plan cron, morning brief cron). Wrote this `DEPLOY.md`.

Verified: `.env` is gitignored, was never tracked in git history, no secrets leaked anywhere in the repo.

### Stage C — SQLite persistence via Railway volume ✅ done (infra only, no commit)

- Railway volume attached at `/data` (~$0.25/GB/month)
- `DATABASE_URL=sqlite:////data/marketcoach.db` (four slashes — `sqlite://` + absolute path)
- Verified by writing a `TEST` watchlist entry, restarting the container, and confirming the entry survived — see the verification script in the Stage C section below
- APScheduler is single-process in-memory so multi-replica is not supported anyway — the single-replica volume constraint is fine
- The deployed DB was created fresh on first boot; the local `marketcoach.db` with ~2 months of paper history was not uploaded (deliberate — wanted a clean slate on the cloud side while the local machine still has the full history for reference)

No code change required. Pure infra.

### Stage D — First deploy + scheduler timezone fix ✅ done (commits `438c3ef`, `cbee6c3`)

**Code change:** added `scheduler_timezone: str = ""` Setting + `_cron_timezone()` helper in [`marketcoach/backend/scheduler.py`](marketcoach/backend/scheduler.py) that resolves the setting through `zoneinfo.ZoneInfo` (stdlib, no `pytz` dependency). Empty string preserves the old behaviour; invalid IANA names log a warning and fall back safely. Both `CronTrigger` calls (weekly plan, morning brief) now carry the resolved timezone. Startup log line includes the active timezone so it's obvious at a glance whether the deploy is configured correctly.

Also committed alongside: a test hygiene fix (`438c3ef`) that makes the test suite hermetic to the local `.env`'s `API_SECRET` via a monkeypatch in `conftest.py`. Without this, setting `API_SECRET` locally (e.g. to match Railway's config) silently breaks 103 tests with 401 errors.

**Railway setup:**
1. New service from GitHub repo, branch `feat/railway-deploy`
2. **Settings → Source → Root Directory:** `marketcoach`
3. **Settings → Build → Start Command:** `uvicorn backend.main:app --host 0.0.0.0 --port 8080`
4. **Settings → Networking → Generate Domain**, target port `8080`
5. Attach volume mounted at `/data` (from Stage C)
6. Set env vars (from the paste block above)
7. Deploy

**Port note:** we use a fixed port `8080` matching Railway's current networking default rather than `$PORT`, because Railway's networking UI silently drifted the target port from 8000 → 8080 mid-session and a fixed port on both sides survives that kind of drift without downtime. If Railway ever drifts the target again, match the start command to the new number. The fully Railway-idiomatic `--port $PORT` approach should also work but we couldn't get its auto-detection to cooperate when the networking panel was explicitly asking for a port value.

**Success criteria in Railway logs** (confirmed working on 2026-04-15):
- `MarketCoach starting up`
- `Broker provider='none' — broker features disabled. Trading endpoints will return 'disconnected'; analysis features work normally.`
- `Scheduler started — tz=America/Sao_Paulo, intelligence=4h, weekly=sun 21:00, morning=mon-fri 06:00, position_poll=every 5min`
- `Application startup complete.`
- `Uvicorn running on http://0.0.0.0:8080`
- Health endpoint at `https://<railway-domain>/health` returns `{"status": "ok", "broker": "none", "broker_connected": false, "alpaca_connected": false}`

The `tz=America/Sao_Paulo` in the scheduler line is the single most important checkpoint: it confirms `SCHEDULER_TIMEZONE` landed correctly. Without it you'd see `tz=server-local` and cron jobs would fire at UTC times (morning brief at 03:00 BRT, etc.).

### Stage E — Point local frontend at Railway

Three coupled changes were needed, not one:

1. **Proxy target** — [`marketcoach/frontend/vite.config.js`](marketcoach/frontend/vite.config.js) now uses `loadEnv` to read `VITE_API_URL` and forwards `/api/*` to whatever that resolves to. Empty or unset falls back to `http://localhost:8000`, preserving the original local-dev behaviour.

2. **Bearer auth on every axios request** — [`marketcoach/frontend/src/main.jsx`](marketcoach/frontend/src/main.jsx) now reads `VITE_API_SECRET` before React mounts and, if non-empty, installs it as `axios.defaults.headers.common['Authorization']`. Without this, every authenticated backend route would return 401 as soon as the backend has a non-empty `API_SECRET` set. Empty secret is a no-op — local dev with `API_SECRET=""` still works unchanged.

3. **Gitignore** — added `.env.local` and `.env.*.local` patterns at the repo root so frontend secrets don't leak. The existing `.env` pattern alone is a literal match and would not have covered `.env.local`.

**Security note on `VITE_API_SECRET`**: `VITE_*` env vars are inlined into the client bundle at build time. This is fine for a local dev server (never exposed to the internet) but **do not build and deploy a frontend with a real API secret baked in** — anyone who loads the page can read it from the JS bundle. Production frontend deploy needs a different auth story (session cookies, OAuth, or a reverse proxy that injects the header). Out of scope for Phase 1 because the frontend is kept local.

**Local setup for Phase 1:**
1. `cp marketcoach/frontend/.env.example marketcoach/frontend/.env.local`
2. Edit `.env.local`:
   ```
   VITE_API_URL=https://<your-railway-domain>
   VITE_API_SECRET=<same value as API_SECRET in Railway env vars>
   ```
3. Restart Vite (`npm run dev` → Ctrl+C → re-run); env vars are read once at dev-server start
4. Load http://localhost:5173
5. Expect: analysis features work, `/portfolio` shows "Broker not configured" (correct — Railway has no broker), trading endpoints return 503 with a clean error

**For trading:** clear or comment out `VITE_API_URL` and `VITE_API_SECRET` in `.env.local`, restart Vite, restart the local backend, start IB Gateway. Local backend has the broker; Railway backend has the 24/7 scheduler. Two deployment targets of the same codebase is unusual but legitimate — it's the simplest way to keep analysis always-on without exposing the home network.

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

- **Railway networking port drift** — Railway's UI silently changed the target port from 8000 → 8080 once during setup. Fixed by binding the start command to 8080 explicitly. If it drifts again, match the start command to the new target; both sides need to agree. Watch for this on future deploys.
- **Equity history discontinuity:** if live mode is later activated, `equity_snapshots` will show a cliff where the paper-account history ends and live-account snapshots begin. Not a deploy blocker. Future fix: partition snapshots by `is_paper`.
- **Bug #1 from HANDOFF.md:** fixed in commit `8affa28` (`place_order` now correctly routes limit orders through `LimitOrderRequest` / `_LimitOrder` instead of silently downgrading to market).
- **Alpaca removal refactor:** queued for a separate branch. Not required for Phase 1 deploy.
- **Frontend auth is dev-only.** `VITE_API_SECRET` in `.env.local` is inlined into the client bundle at build time. Safe for local dev (Vite dev server isn't exposed) but unsafe for any future frontend deploy. When the frontend eventually moves to a hosting provider, the auth story needs to change — options: session-cookie flow, OAuth, or a reverse proxy that injects the header server-side. Not a Phase 1 concern.

## Phase 1 completion status

All five stages done and verified in production as of 2026-04-16:

| Stage | Scope | Status | Commit |
|---|---|---|---|
| A | Broker-degradation audit + `BROKER_PROVIDER=none` | ✅ verified in prod | `da52879` |
| B | Env var inventory + rewritten `.env.example` + this doc | ✅ | `cae5f25` |
| C | Railway volume + SQLite persistence across restarts | ✅ verified with `TEST` ticker survival across restart | (infra) |
| D | `SCHEDULER_TIMEZONE` + timezone-aware cron + test hygiene fix | ✅ `tz=America/Sao_Paulo` in prod logs | `cbee6c3`, `438c3ef` |
| E | `vite.config.js` proxy env var + frontend bearer auth + `.gitignore` | ✅ | (this commit) |

Next phases — out of scope for this branch:

- Alpaca removal refactor (clean up the now-unused broker path)
- Frontend deploy with a real auth story
- Postgres migration (only if SQLite volume size ever becomes a concern)
- CI via GitHub Actions (pytest on push)
