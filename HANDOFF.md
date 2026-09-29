# MarketCoach — session handoff

Paste this into a new Claude chat as context so it can pick up where the
previous session left off. Everything here is state that's hard to infer
just by reading the code.

---

## Who I am (Ciro Beduschi Domingos)

- Senior Data Analyst, 15 years at Volvo Group (Curitiba, Brazil). Stack:
  Python, SQL, Power BI, Databricks, scikit-learn, XGBoost, FastAPI,
  Streamlit, Docker. Learning: dbt, DuckDB, analytics engineering, LLM
  orchestration, production ML pipelines.
- Goal: land a **remote international contract** as Analytics Engineer /
  Senior Data Analyst. Target $80k–$130k USD. Applying now while building
  portfolio projects. Every feature = an interview story.
- Windows 11, PowerShell, `py -3.12` (not `python`). No Docker.
- How I want to work:
  - Make architectural decisions yourself. Tell me what and why — don't
    ask me to pick between options unless there's no right answer.
  - Teaching layer always on: explain non-trivial concepts once, flag
    portfolio/interview signals, connect new ideas to things I know.
  - Parallelize independent work (agents, worktrees). Concise + direct.
  - Small changes commit straight to main. Bigger features in branches.

## What MarketCoach is

Personal AI trading advisor at `C:\repo\market-ai-agent\marketcoach`.
Monitors markets, generates theses, provides interactive advisor chat,
paper-trades via IBKR (or Alpaca). It's one of my portfolio projects and
also the one I actually use.

**Stack:** FastAPI + SQLAlchemy + APScheduler (Python 3.12) / React 18 +
Vite + Tailwind + Recharts / Anthropic SDK agentic loop / ib_insync for
IBKR / yfinance / SQLite / pytest.

## Architecture notes (the non-obvious parts)

- **Broker abstraction** (`backend/brokers/`): `BrokerClient` ABC with
  `AlpacaBroker` + `IBKRBroker` implementations. Chosen by
  `settings.broker_provider`. Motivation: Alpaca stopped accepting
  Brazilian residents for live; IBKR is the only serious retail broker
  that does. The ABC lets us swap without touching agents or endpoints.
- **IBKR thread-affinity fix:** `ib_insync` is single-threaded and bound
  to the event loop that called `ib.connect()`. FastAPI's anyio
  threadpool would otherwise hang on the second call. Fix: a dedicated
  daemon thread owns the event loop; all IB ops go through
  `run_coroutine_threadsafe`. See `_run_on_broker_loop` in
  `backend/brokers/ibkr.py`.
- **Advisor = TradingAdvisorAgent**, NOT CoachAgent. CoachAgent is the
  educational-only variant. Advisor is decisive, knows about live
  account state, and can emit structured `trade-proposal` blocks that
  the frontend turns into one-click Execute cards.
- **System prompt split for caching**: the advisor prompt is in two
  blocks — a stable prefix (banner + role + framework + tools) marked
  `cache_control: ephemeral`, and a dynamic suffix (account snapshot).
  Cache key dimensions: mode (paper/live) × addendum (on/off). Repeat
  calls within ~5 min pay ~10% input cost on the stable block.
- **Safety gate pattern** in `/orders/confirm`: schema → bracket gate →
  live-mode confirm → broker connected → daily cap → whitelist → 20%
  rule → broker submit → persist → journal. Every gate is independent
  and persistable as a rejection row.
- **Auto-whitelist for advisor trades:** when `/orders/confirm` gets
  both `advisor_session_id` AND `rationale`, the ticker is auto-added
  to the watchlist. Dual-signal check prevents direct-API bypass.
- **Journal discipline:** `user_thesis` required before execute,
  `user_lesson` required after close. Enforced at the schema and UI
  layers — the whole point is the discipline, so these are never
  optional.
- **News reactions reused as notification surface:** every alerting
  path (position changes, news signals, now price alerts) writes to
  `news_reactions` with a `trigger_reason` discriminator. The Notifs
  tab/nav badge/mark-read flow works for all of them automatically.
- **Three-layer close-price fallback** when a position disappears:
  executed_orders fill → snapshot → yfinance → open_price. Why:
  IBKR paper sometimes returns None for `current_price`.

## What was built in the most recent session

### Price alerts (commit dbfb9a6)
- `PriceAlert` model + CRUD (`create / list / group_by_ticker /
  mark_triggered / delete`)
- `orchestrator._check_price_alerts()` called from the existing 5-min
  position poll. One yfinance quote per unique ticker (not per alert).
  Fires a `news_reactions` row with `trigger_reason="price_alert"` and
  marks the alert inactive — one-shot.
- `GET/POST/DELETE /alerts` API with Pydantic validation
- Portfolio page Alerts section: add form + active/triggered list
- Advisor prompt updated: "these exist, you cannot set them yourself,
  direct the user to the Portfolio page."
- 18 tests. **Why this shipped:** advisor said "set your alert and
  wait" and we didn't actually have alerts. Classic "don't let the
  model hallucinate capabilities" fix — build the feature AND tell the
  model about it.

### Bracket orders (commit d7c88ff)
- `BrokerClient.place_bracket_order` added to ABC
- **Alpaca:** `LimitOrderRequest` with `OrderClass.BRACKET` + attached
  `TakeProfitRequest` / `StopLossRequest`, TIF=GTC so legs persist
  overnight.
- **IBKR:** `ib.bracketOrder()` helper returns `[parent, tp, sl]`
  bound by `parentId`. All three placed on the dedicated broker thread.
- `ExecutedOrder` gains `order_class`, `stop_loss_price`,
  `take_profit_price` columns for audit trail.
- `OrderConfirmRequest` gains optional `stop_loss` + `target_1`.
  `/orders/confirm` routes to `place_bracket_order` when both are set.
- **v1 constraints (enforced in endpoint):** `side='buy'` only,
  `order_type='limit'` only, `stop_loss < limit_price < target_1`,
  both-or-neither.
- `ConfirmTradeModal` shows "AUTO BRACKET" badge when eligible, else
  falls back to the old "manual exits to set" list with a note
  explaining why bracket was skipped.
- Advisor prompt: `stop_loss` + `target_1` are now auto-executed, not
  descriptive. Emit both or neither.
- 11 tests. **Interview angle:** OCO / parentId pattern, atomic
  coupling — the cheap alternative (place entry, then place stops in a
  second call) leaves a window where the position exists without
  exits. Also a clean example of Strategy pattern on an ABC.

### Earlier-in-session work still worth knowing about
- Dedicated SellModal for one-click position management with 25/50/75/100
  presets and required thesis textarea.
- Equity curve chart + per-trade cumulative P&L chart on the Dashboard.
- Journal fix: `_update_journal_from_changes` was running AFTER the
  dedupe early-return, so closed journal entries stayed "open". Moved
  before the dedupe.
- 20% rule fix: scoped to `side=='buy'` only. Sells of existing longs
  always reduce risk and shouldn't be rejected by a position-sizing gate.
- `utcnow()` deprecation → `datetime.now(timezone.utc)` everywhere.
- Current-price in IBKR `get_positions` populated via yfinance (was
  returning None after the thread-affinity refactor, showing $0.00 in
  the UI).
- `start-marketcoach.bat` at the repo root: double-click launcher that
  opens backend + frontend in separate cmd windows and opens the
  browser. IB Gateway still has to be started manually (GUI login).

## Feedback rules learned this session

- **"commit small changes to main directly"** — don't open a branch +
  PR for every tiny fix. Feature branches for bigger stuff only.
- **"don't mock the database in tests, we got burned last quarter"** —
  integration tests hit a real in-memory SQLite, not mocks. Mock the
  broker, not the DB.
- **The advisor should never claim capabilities it doesn't have.**
  When the model hallucinated alerts, the fix was to build them AND
  update the prompt to describe what exists / what it can't do.

## Open follow-ups (not done)

1. **Pre-existing bug:** `place_order` in both Alpaca and IBKR is
   hardcoded to market orders, despite the API schema accepting
   `order_type='limit'`. Non-bracket limit orders silently submit as
   market. One-line fix next time you're in `alpaca.py`. Bracket path
   doesn't hit it.
2. **Target 2 is still descriptive-only.** Real scale-out means: when
   target_1 fills, move stop to breakeven and arm target_2. Worth
   building only once you actually use it.
3. **Pending limit orders visibility** in the Portfolio UI — currently
   you can submit a GTC limit and then have no easy way to see it or
   cancel it from the app.
4. **Quick-quote widget** on the Portfolio or Dashboard page — right
   now you have to go to the Advisor to check a price.
5. **CI/CD via GitHub Actions** for pytest on push. Discussed, not built.

## How to restart after a reboot

Double-click `C:\repo\market-ai-agent\start-marketcoach.bat`. It:
1. Reminds you to start IB Gateway manually (port 4002 paper)
2. Opens backend in a cmd window: `py -3.12 -m uvicorn backend.main:app --reload`
3. Opens frontend in a cmd window: `npm run dev`
4. Opens http://localhost:5173 after ~6s

Close the two service windows to stop.

## Relevant paths

- Repo: `C:\repo\market-ai-agent`
- App: `C:\repo\market-ai-agent\marketcoach`
- Backend: `marketcoach\backend` (FastAPI, SQLAlchemy, agents, brokers)
- Frontend: `marketcoach\frontend` (React + Vite)
- Tests: `marketcoach\tests` (306 passing as of d7c88ff)
- DB: `marketcoach\marketcoach.db` (SQLite)
- GitHub: github.com/cirobdomingos-cyber/market-agent (main branch)
