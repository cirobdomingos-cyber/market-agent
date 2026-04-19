"""
MarketCoach -- FastAPI application entry point.

Routes:
  GET  /health                  -- liveness check
  GET  /signals                 -- paginated signal feed
  GET  /theses                  -- paginated theses (open or all)
  GET  /accuracy                -- thesis accuracy stats
  POST /pipeline/run            -- trigger intelligence pipeline manually
  POST /chat                    -- send a message to the coach
  GET  /chat/{session_id}       -- fetch session message history
  GET  /portfolio               -- paper positions + account (Alpaca)
  GET  /portfolio/orders        -- recent order history
  GET  /profile                 -- user memory / preferences
  POST /profile                 -- manually set a user memory
  POST /backtest/run            -- start a new backtest simulation
  GET  /backtests               -- list past backtest runs
  GET  /backtest/{run_id}       -- fetch full backtest results
  GET  /events                  -- upcoming earnings + FOMC dates for watchlist tickers
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend.config import settings
from backend.db import get_db, init_db
from backend.db import crud
from backend.agents.orchestrator import Orchestrator
from backend.scheduler import start_scheduler, stop_scheduler
from backend.brokers import init_broker, get_broker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# -- Auth dependency -----------------------------------------------------------

bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> str:
    """
    Simple bearer-token auth. Set API_SECRET in .env to enable.
    When API_SECRET is empty (dev mode), auth is bypassed.
    """
    if not settings.api_secret:
        return "dev"
    if credentials is None or credentials.credentials != settings.api_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return credentials.credentials


# -- App lifecycle -------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MarketCoach starting up")
    init_db()

    # Seed the watchlist from DEFAULT_WATCHLIST on first startup. This
    # migrates the old env-var-based watchlist into the database so the
    # user can add/remove tickers from the UI without editing .env and
    # restarting. If the watchlist table already has rows, seed is a
    # no-op — the user's existing picks are preserved.
    from backend.db import SessionLocal
    _seed_db = SessionLocal()
    try:
        from backend.db import crud as _crud
        _seeded = _crud.seed_watchlist_if_empty(_seed_db, settings.watchlist)
        if _seeded:
            logger.info("Seeded %d tickers into watchlist from env", _seeded)
    finally:
        _seed_db.close()

    # Initialize the broker singleton. Provider is chosen by settings.broker_provider.
    # Trading mode is gated by the same dual switch regardless of provider:
    # alpaca_paper=False is not enough — the confirmation phrase must also be
    # set. If the user sets paper=False without the phrase, we refuse to start
    # in live mode and fall back to paper with a loud warning.
    is_live = settings.is_live_mode
    if settings.alpaca_paper is False and not is_live:
        logger.warning(
            "ALPACA_PAPER=false but ALPACA_LIVE_CONFIRMATION is missing or "
            "incorrect. Refusing to start in live mode — falling back to "
            "paper. Set ALPACA_LIVE_CONFIRMATION='%s' to enable live.",
            settings.LIVE_CONFIRMATION_PHRASE,
        )

    provider = settings.broker_provider.lower().strip()
    if provider == "ibkr":
        init_broker(
            "ibkr",
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id,
            paper=not is_live,
        )
    elif provider == "none":
        # Explicit opt-out — used when the backend runs somewhere it can't
        # reach a broker (cloud-hosted analysis service, test environment,
        # etc.). Analysis features work normally; trading endpoints return
        # "disconnected". Factory logs its own info line when this hits.
        init_broker("none")
    elif provider == "alpaca":
        # Explicit error rather than a silent fallback. Anyone still setting
        # BROKER_PROVIDER=alpaca from a pre-removal .env should see the
        # message and switch rather than get confusing behaviour.
        logger.error(
            "BROKER_PROVIDER=alpaca is no longer supported. The Alpaca "
            "integration was removed because Alpaca doesn't onboard "
            "Brazilian residents for live trading. Set BROKER_PROVIDER=ibkr "
            "for Interactive Brokers, or BROKER_PROVIDER=none to disable "
            "broker features entirely. Startup will continue with broker "
            "features disabled until you update .env."
        )
    else:
        logger.warning(
            "Unknown BROKER_PROVIDER='%s'. Broker features disabled.",
            settings.broker_provider,
        )

    if get_broker() is not None:
        if is_live:
            logger.warning(
                "═══════════════════════════════════════════════════════════"
            )
            logger.warning(
                "  BROKER (%s) INITIALISED IN LIVE MODE", provider.upper()
            )
            logger.warning("  Real capital is at risk.")
            logger.warning(
                "═══════════════════════════════════════════════════════════"
            )
        else:
            logger.info(
                "Broker (%s) initialised in paper mode", provider
            )

    start_scheduler()
    yield
    stop_scheduler()
    logger.info("MarketCoach shut down")


app = FastAPI(
    title="MarketCoach API",
    description="AI-powered market intelligence and personal finance coach",
    version="0.2.0",
    lifespan=lifespan,
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Return clean JSON for unhandled errors instead of raw tracebacks."""
    error_name = type(exc).__name__
    if "RateLimitError" in error_name or "429" in str(exc):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limited by the AI provider. Please wait a minute and try again."},
        )
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Check the server logs for details."},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Request/response models ---------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    message: str = Field(min_length=1, max_length=4000)


class WatchlistAddRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=5, pattern=r"^[A-Za-z]{1,5}$")
    notes: Optional[str] = Field(default=None, max_length=500)


class PriceAlertRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=5, pattern=r"^[A-Za-z]{1,5}$")
    condition: str = Field(pattern=r"^(above|below)$")
    target_price: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=500)


class OrderConfirmRequest(BaseModel):
    """
    A user-confirmed trade proposal. The agent can never construct this
    directly — it must come from a click on the Execute button in the UI,
    which forwards a parsed trade-proposal block from an advisor message.
    """
    ticker: str = Field(min_length=1, max_length=5, pattern=r"^[A-Z]{1,5}$")
    side: str = Field(pattern=r"^(buy|sell)$")
    qty: float = Field(gt=0, le=10_000)
    order_type: str = Field(pattern=r"^(market|limit)$")
    limit_price: Optional[float] = Field(default=None, gt=0)
    # Bracket fields — optional. When BOTH are set, /orders/confirm routes
    # the request to place_bracket_order so the exits are wired at the
    # broker instead of left as manual to-dos. See the bracket gate in the
    # endpoint for the full set of rules (buy-only, limit-only, level order).
    stop_loss: Optional[float] = Field(default=None, gt=0)
    target_1: Optional[float] = Field(default=None, gt=0)
    # Scale-out bracket: how many shares target_1 sells. None or == qty
    # means classic all-out bracket (take-profit sells the whole position
    # and the stop auto-cancels via OCA). target_qty < qty means the
    # take-profit sells only that portion and the rest runs with a stop
    # at the same price — when target_1 fills, the runner's stop moves
    # to the entry price (breakeven) via the state machine in the
    # position poll. Must be a positive integer strictly less than qty
    # to enable scale-out; anything else falls back to all-out.
    target_qty: Optional[float] = Field(default=None, gt=0)
    rationale: Optional[str] = Field(default=None, max_length=500)
    advisor_session_id: Optional[str] = Field(default=None, max_length=64)
    # Live mode safety: this field MUST be present and True when the system
    # is in live mode. The frontend modal collects an explicit checkbox for it.
    confirm_live_capital: bool = False
    # Trade journal — the user MUST type their own thesis before executing.
    # The whole point of the journal is the discipline; if these were optional
    # they'd always be skipped. The UI enforces a min length too.
    user_thesis: str = Field(min_length=10, max_length=500)
    user_disagreement: Optional[str] = Field(default=None, max_length=500)


class PipelineRunResponse(BaseModel):
    status: dict
    message: str


class MemoryRequest(BaseModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    value: str = Field(min_length=1, max_length=1000)
    category: str = Field(default="general", pattern=r"^(profile|preference|observation|general)$")


class BacktestRequest(BaseModel):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(default=100_000.0, ge=1_000, le=10_000_000)
    watchlist: list[str] | None = None
    decision_interval_days: int = Field(default=7, ge=1, le=30)
    risk_tolerance: str = Field(default="moderate", pattern=r"^(conservative|moderate|aggressive)$")


# -- Routes --------------------------------------------------------------------

@app.get("/health")
def health():
    broker = get_broker()
    return {
        "status": "ok",
        "version": "0.2.0",
        # Field name kept as alpaca_connected for frontend backwards-compat;
        # the value now reflects whichever broker is active (Alpaca or IBKR).
        "broker": settings.broker_provider,
        "broker_connected": broker is not None and broker.is_connected(),
        "alpaca_connected": broker is not None and broker.is_connected(),
    }


@app.get("/signals")
def get_signals(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ticker: str | None = None,
    hours: int = Query(default=24, ge=1, le=720),
    db: Session = Depends(get_db),
):
    if ticker:
        signals = crud.get_recent_signals(db, hours=hours, ticker=ticker.upper())
    else:
        signals = crud.get_signals_page(db, skip=skip, limit=limit)

    return [
        {
            "id": s.id,
            "ticker": s.ticker,
            "sentiment": s.sentiment,
            "confidence": s.confidence,
            "source": s.source,
            "headline": s.headline,
            "created_at": s.created_at.isoformat(),
        }
        for s in signals
    ]


@app.get("/theses")
def get_theses(
    open_only: bool = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    theses = crud.get_open_theses(db) if open_only else crud.get_theses_page(db, skip, limit)

    return [
        {
            "id": t.id,
            "ticker": t.ticker,
            "direction": t.direction,
            "confidence": t.confidence,
            "timeframe": t.timeframe,
            "reasoning": t.reasoning,
            "key_risks": json.loads(t.key_risks) if t.key_risks else [],
            "created_at": t.created_at.isoformat(),
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "outcome": t.outcome,
            "accuracy": t.accuracy,
        }
        for t in theses
    ]


@app.get("/accuracy")
def get_accuracy(db: Session = Depends(get_db)):
    return crud.get_accuracy_stats(db)


@app.get("/performance")
def get_performance(db: Session = Depends(get_db)):
    """
    Realised-trade performance stats for the Performance dashboard.

    LIVE ONLY by design — paper trades are excluded because "did the
    advisor help?" is only meaningfully answerable on real-money
    outcomes. Paper results don't carry the psychological or execution
    reality of a live bracket. If the user ever wants to see paper
    retrospectives, that's a separate endpoint; not mixing them here.

    Scope: closed TradeJournalEntry rows whose originating ExecutedOrder
    row has is_paper=False. Manual trades that bypassed /orders/confirm
    have no open_executed_order_id and are not counted — rare in
    practice, and the alternative (trusting a detection-time is_paper
    flag we don't currently store on the journal row) is brittle.

    Returns a single JSON blob with headline KPIs + the most-recent 50
    closed trades for the table view. Frontend computes no aggregates —
    everything here is wire-ready.
    """
    from backend.db.models import TradeJournalEntry, ExecutedOrder

    closed = (
        db.query(TradeJournalEntry)
        .join(
            ExecutedOrder,
            ExecutedOrder.id == TradeJournalEntry.open_executed_order_id,
        )
        .filter(
            TradeJournalEntry.status == "closed",
            TradeJournalEntry.pnl_amount.isnot(None),
            ExecutedOrder.is_paper.is_(False),
        )
        .order_by(TradeJournalEntry.closed_at.desc())
        .all()
    )

    open_count = (
        db.query(TradeJournalEntry)
        .join(
            ExecutedOrder,
            ExecutedOrder.id == TradeJournalEntry.open_executed_order_id,
        )
        .filter(
            TradeJournalEntry.status == "open",
            ExecutedOrder.is_paper.is_(False),
        )
        .count()
    )

    def _serialise_trade(t: TradeJournalEntry) -> dict:
        return {
            "id": t.id,
            "ticker": t.ticker,
            "side": t.side,
            "qty": t.qty,
            "open_price": t.open_price,
            "close_price": t.close_price,
            "pnl_amount": t.pnl_amount,
            "pnl_pct": t.pnl_pct,
            "days_held": t.days_held,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "advisor_session_id": t.advisor_session_id,
            "user_thesis": (t.user_thesis or "")[:200],
        }

    if not closed:
        # Render the dashboard with zeroes/nulls — page layout stays
        # identical but every KPI card shows "—" until trades land.
        return {
            "closed_trades_count": 0,
            "open_positions_count": open_count,
            "winners_count": 0,
            "losers_count": 0,
            "win_rate_pct": None,
            "total_realized_pnl": 0.0,
            "avg_win_dollar": None,
            "avg_loss_dollar": None,
            "largest_win": None,
            "largest_loss": None,
            "profit_factor": None,
            "avg_hold_days": None,
            "advised_trades_count": 0,
            "advised_win_rate_pct": None,
            "recent_trades": [],
            "equity_curve": [],
        }

    winners = [t for t in closed if t.pnl_amount > 0]
    losers = [t for t in closed if t.pnl_amount < 0]
    # Zero-pnl trades (very rare, usually a breakeven exit on the runner
    # after T1 fill) count toward total but not toward winners/losers.

    n = len(closed)
    n_winners = len(winners)
    n_losers = len(losers)

    gross_wins = sum(t.pnl_amount for t in winners) if winners else 0.0
    gross_losses = sum(abs(t.pnl_amount) for t in losers) if losers else 0.0

    # Attribution — were the advisor's trades better or worse?
    advised = [t for t in closed if t.advisor_session_id]
    advised_winners = [t for t in advised if t.pnl_amount > 0]

    hold_days = [t.days_held for t in closed if t.days_held is not None]

    return {
        "closed_trades_count": n,
        "open_positions_count": open_count,
        "winners_count": n_winners,
        "losers_count": n_losers,
        # Win rate treats breakeven trades as neither — denominator
        # is all closed trades, so a 50/50 split with 0 breakevens
        # shows 50%, but a 50/49/1 split shows 50% too. Intentional:
        # breakevens are a separate category (it's a win to not lose).
        "win_rate_pct": round(n_winners / n * 100, 1),
        "total_realized_pnl": round(sum(t.pnl_amount for t in closed), 2),
        "avg_win_dollar": (
            round(gross_wins / n_winners, 2) if n_winners else None
        ),
        "avg_loss_dollar": (
            round(-gross_losses / n_losers, 2) if n_losers else None
        ),
        "largest_win": (
            round(max(t.pnl_amount for t in winners), 2)
            if winners else None
        ),
        "largest_loss": (
            round(min(t.pnl_amount for t in losers), 2)
            if losers else None
        ),
        # Profit factor = gross wins / gross losses. Above 1 is
        # profitable, above 2 is excellent. Undefined when no losers
        # (statistically meaningless with a tiny sample anyway).
        "profit_factor": (
            round(gross_wins / gross_losses, 2) if gross_losses > 0 else None
        ),
        "avg_hold_days": (
            round(sum(hold_days) / len(hold_days), 1) if hold_days else None
        ),
        "advised_trades_count": len(advised),
        "advised_win_rate_pct": (
            round(len(advised_winners) / len(advised) * 100, 1)
            if advised else None
        ),
        "recent_trades": [_serialise_trade(t) for t in closed[:50]],
        # Cumulative realised P&L curve for the chart. Ordered oldest
        # → newest so the line reads left-to-right like every other
        # time-series view in the app. Each point is the running sum
        # after that trade closed. No fills between trades — the line
        # only steps when something actually realises P&L.
        "equity_curve": _build_equity_curve(closed),
    }


def _build_equity_curve(closed: list) -> list[dict]:
    """
    Turn a list of closed trades into a cumulative realised-P&L series.
    Oldest first so the chart runs left-to-right. Each point carries:
      - x: ISO timestamp when the trade closed
      - cumulative_pnl: running sum of pnl_amount up to and including
        this trade
      - trade_pnl: this trade's contribution (for tooltip detail)
      - ticker: the closed trade's ticker (tooltip)
    """
    by_close = sorted(closed, key=lambda t: t.closed_at or t.opened_at)
    running = 0.0
    points = []
    for t in by_close:
        if t.pnl_amount is None:
            continue
        running += t.pnl_amount
        points.append({
            "x": (t.closed_at or t.opened_at).isoformat(),
            "cumulative_pnl": round(running, 2),
            "trade_pnl": round(t.pnl_amount, 2),
            "ticker": t.ticker,
        })
    return points


@app.post("/pipeline/run", response_model=PipelineRunResponse)
def run_pipeline(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Manually trigger the intelligence pipeline (news + analysis + resolution)."""
    orchestrator = Orchestrator(db)
    results = orchestrator.run_intelligence_pipeline()
    status = orchestrator.get_pipeline_status(results)

    all_ok = all(r.success for r in results.values())
    return PipelineRunResponse(
        status=status,
        message="Pipeline completed successfully" if all_ok else "Pipeline completed with errors",
    )


@app.post("/chat")
def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Send a message to the coach and get a response."""
    orchestrator = Orchestrator(db)

    theses = crud.get_open_theses(db, limit=5)
    signals = crud.get_recent_signals(db, hours=4)
    accuracy = crud.get_accuracy_stats(db)

    # Load user profile for personalized coaching
    user_profile = orchestrator.get_user_profile()

    context = {
        "session_id": request.session_id,
        "user_message": request.message,
        "portfolio": [],  # populated by orchestrator from Alpaca
        "theses": [
            {
                "ticker": t.ticker,
                "direction": t.direction,
                "confidence": t.confidence,
                "timeframe": t.timeframe,
                "reasoning": t.reasoning[:200],
            }
            for t in theses
        ],
        "signals": [
            {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
            for s in signals[:10]
        ],
        "accuracy": accuracy,
        "user_profile": user_profile,
    }

    result = orchestrator.run_coach(context)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return {
        "session_id": result.data.get("session_id"),
        "response": result.data.get("reply", ""),
    }


def _serialise_news_reaction(r) -> dict:
    return {
        "id": r.id,
        "ticker": r.ticker,
        "headline": r.headline,
        "content": r.content,
        "trigger_reason": r.trigger_reason,
        "status": r.status,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@app.get("/news-reactions")
def list_news_reactions(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """List news reactions newest first. Optional status filter (unread/read/dismissed/failed)."""
    if status is not None and status not in ("unread", "read", "dismissed", "failed"):
        raise HTTPException(status_code=422, detail="Invalid status filter")
    reactions = crud.list_news_reactions(
        db, status=status, limit=min(max(1, limit), 200)
    )
    return [_serialise_news_reaction(r) for r in reactions]


@app.get("/news-reactions/unread-count")
def get_unread_reaction_count(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Lightweight count for the nav badge — polled every 60s by the UI."""
    return {"unread": crud.count_unread_reactions(db)}


@app.post("/news-reactions/{reaction_id}/read")
def mark_reaction_read(
    reaction_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    reaction = crud.update_reaction_status(db, reaction_id, "read")
    if reaction is None:
        raise HTTPException(status_code=404, detail="Reaction not found")
    return _serialise_news_reaction(reaction)


@app.post("/news-reactions/{reaction_id}/dismiss")
def dismiss_reaction(
    reaction_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    reaction = crud.update_reaction_status(db, reaction_id, "dismissed")
    if reaction is None:
        raise HTTPException(status_code=404, detail="Reaction not found")
    return _serialise_news_reaction(reaction)


@app.get("/mode")
def get_trading_mode():
    """
    Return the current trading mode so the frontend can render a live-mode
    banner. Intentionally unauthenticated and side-effect-free — this is the
    one status check that should always work, even before login.
    """
    return {
        "mode": settings.trading_mode,
        "is_live": settings.is_live_mode,
        "paper_flag": settings.alpaca_paper,
        "confirmation_set": bool(settings.alpaca_live_confirmation),
    }


def _serialise_weekly_plan(plan) -> dict:
    return {
        "id": plan.id,
        "session_id": plan.session_id,
        "content": plan.content,
        "status": plan.status,
        "error": plan.error,
        "trigger": plan.trigger,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


@app.get("/weekly-plan/latest")
def get_latest_weekly_plan(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Return the most recently generated weekly plan, or 404 if none exist."""
    plan = crud.get_latest_weekly_plan(db)
    if plan is None:
        raise HTTPException(status_code=404, detail="No weekly plan generated yet")
    return _serialise_weekly_plan(plan)


@app.get("/weekly-plan")
def list_weekly_plans(
    limit: int = 10,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """List recent weekly plans, newest first."""
    plans = crud.list_weekly_plans(db, limit=min(max(1, limit), 50))
    return [_serialise_weekly_plan(p) for p in plans]


def _serialise_morning_brief(brief) -> dict:
    return {
        "id": brief.id,
        "session_id": brief.session_id,
        "content": brief.content,
        "status": brief.status,
        "error": brief.error,
        "trigger": brief.trigger,
        "created_at": brief.created_at.isoformat() if brief.created_at else None,
    }


@app.get("/morning-brief/latest")
def get_latest_morning_brief(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Return the most recent morning brief, or 404 if none exist yet."""
    brief = crud.get_latest_morning_brief(db)
    if brief is None:
        raise HTTPException(status_code=404, detail="No morning brief generated yet")
    return _serialise_morning_brief(brief)


@app.get("/morning-brief")
def list_morning_briefs(
    limit: int = 14,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """List recent morning briefs, newest first. Default 14 = ~2 weeks of weekdays."""
    briefs = crud.list_morning_briefs(db, limit=min(max(1, limit), 60))
    return [_serialise_morning_brief(b) for b in briefs]


@app.post("/morning-brief/run")
def run_morning_brief_now(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Manually trigger a morning brief generation — used by the 'Run now' UI button."""
    orchestrator = Orchestrator(db)
    result = orchestrator.run_morning_brief(trigger="manual")
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    brief = crud.get_latest_morning_brief(db)
    return _serialise_morning_brief(brief)


@app.post("/weekly-plan/run")
def run_weekly_plan_now(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Manually trigger a weekly plan generation. Used by the 'Run now' UI button."""
    orchestrator = Orchestrator(db)
    result = orchestrator.run_weekly_plan(trigger="manual")
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    plan = crud.get_latest_weekly_plan(db)
    return _serialise_weekly_plan(plan)


@app.post("/advisor")
def advisor(
    request: ChatRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Send a message to the trading advisor and get a decisive response."""
    orchestrator = Orchestrator(db)

    theses = crud.get_open_theses(db, limit=5)
    signals = crud.get_recent_signals(db, hours=4)

    context = {
        "session_id": request.session_id,
        "user_message": request.message,
        # Only the user-facing chat path enables structured trade proposals.
        # Briefs/news-reactions/weekly-plans intentionally never emit them —
        # nobody clicks Execute on a 06:00 brief while half-asleep.
        "enable_trade_proposals": True,
        "theses": [
            {
                "ticker": t.ticker,
                "direction": t.direction,
                "confidence": t.confidence,
                "timeframe": t.timeframe,
                "reasoning": t.reasoning[:200],
            }
            for t in theses
        ],
        "signals": [
            {"ticker": s.ticker, "sentiment": s.sentiment, "headline": s.headline}
            for s in signals[:10]
        ],
    }

    result = orchestrator.run_advisor(context)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return {
        "session_id": result.data.get("session_id"),
        "response": result.data.get("reply", ""),
    }


@app.get("/chat/{session_id}")
def get_chat_history(session_id: str, db: Session = Depends(get_db)):
    messages = crud.get_session_messages(db, session_id)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]


# -- Portfolio -----------------------------------------------------------------

@app.get("/portfolio")
def get_portfolio(_auth: str = Depends(require_auth)):
    """Return positions and account summary from the active broker."""
    client = get_broker()
    if client is None:
        return {
            "account": {"status": "disconnected", "message": "Broker not configured"},
            "positions": [],
        }
    return {
        "account": client.get_account(),
        "positions": client.get_positions(),
    }


@app.get("/watchlist")
def get_watchlist(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Return all tickers currently on the watchlist, alphabetically."""
    rows = crud.list_watchlist_tickers(db)
    return [
        {
            "ticker": r.ticker,
            "notes": r.notes,
            "added_at": r.added_at.isoformat() if r.added_at else None,
        }
        for r in rows
    ]


@app.post("/watchlist")
def add_watchlist_ticker_route(
    request: WatchlistAddRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Add a ticker to the watchlist. Idempotent — re-adding an existing
    ticker just updates its notes (if provided). Ticker is uppercased.
    """
    row = crud.add_watchlist_ticker(
        db, ticker=request.ticker, notes=request.notes
    )
    return {
        "ticker": row.ticker,
        "notes": row.notes,
        "added_at": row.added_at.isoformat() if row.added_at else None,
    }


@app.delete("/watchlist/{ticker}")
def delete_watchlist_ticker_route(
    ticker: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Remove a ticker from the watchlist. 404 if not found."""
    ok = crud.remove_watchlist_ticker(db, ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not on watchlist")
    return {"deleted": ticker.upper()}


def _serialise_alert(a) -> dict:
    return {
        "id": a.id,
        "ticker": a.ticker,
        "condition": a.condition,
        "target_price": a.target_price,
        "note": a.note,
        "active": a.active,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
        "triggered_price": a.triggered_price,
    }


@app.get("/alerts")
def list_alerts(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """List price alerts newest-first. active_only=true hides triggered ones."""
    alerts = crud.list_price_alerts(db, active_only=active_only)
    return [_serialise_alert(a) for a in alerts]


@app.post("/alerts")
def create_alert(
    request: PriceAlertRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Create a price alert. Fires via the 5-min position poll when current
    price meets the condition. One-shot — after firing, the alert is
    marked inactive and a news_reactions notification appears in the
    Notifications tab with the price data.
    """
    alert = crud.create_price_alert(
        db,
        ticker=request.ticker.upper(),
        condition=request.condition,
        target_price=request.target_price,
        note=request.note,
        active=True,
    )
    return _serialise_alert(alert)


@app.delete("/alerts/{alert_id}")
def delete_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Delete an alert (active or triggered). 404 if not found."""
    ok = crud.delete_price_alert(db, alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"deleted": alert_id}


@app.get("/equity-history")
def get_equity_history(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Return equity time series over the last N days (default 30, max 365).

    One row per polling cycle — the resolution is bounded by the
    position poll interval (default 5 min → ~288 points/day). The
    frontend uses this for the Dashboard's equity line chart and the
    today/week/all-time P&L summary cards.

    Returns rows ordered oldest → newest so the chart reads left-to-right.
    Empty list if there's no history yet (e.g. fresh install).
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = crud.list_equity_snapshots(db, since=cutoff)
    return [
        {
            "timestamp": r.snapshot_at.isoformat() if r.snapshot_at else None,
            "equity": r.equity,
            "buying_power": r.buying_power,
            "cash": r.cash,
            "portfolio_value": r.portfolio_value,
        }
        for r in rows
    ]


@app.get("/portfolio/orders")
def get_order_history(
    limit: int = Query(default=20, ge=1, le=100),
    _auth: str = Depends(require_auth),
):
    """Return recent order history from the active broker."""
    client = get_broker()
    if client is None:
        return []
    return client.get_order_history(limit=limit)


@app.get("/orders/pending")
def get_pending_orders(
    _auth: str = Depends(require_auth),
):
    """
    Return working orders (submitted but not yet filled or cancelled) from
    the active broker. Distinct from /portfolio/orders, which is historical.
    Used by the Portfolio page's Pending Orders section.
    """
    client = get_broker()
    if client is None:
        return []
    return client.get_pending_orders()


@app.delete("/orders/{order_id}")
def cancel_pending_order(
    order_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Cancel a working order by its broker-side permanent ID. For brackets,
    cancelling the parent auto-cancels the OCO legs at the broker — we
    don't iterate legs ourselves. Also updates the matching executed_orders
    row to status='cancelled' so history stays consistent.
    """
    client = get_broker()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Broker not configured — cannot cancel orders.",
        )

    result = client.cancel_order(order_id)
    if "error" in result:
        # Not-found from the broker (order already filled/cancelled) →
        # return 404 so the frontend can distinguish "gone" from "broken".
        if "not found" in result["error"].lower():
            raise HTTPException(status_code=404, detail=result["error"])
        raise HTTPException(status_code=502, detail=result["error"])

    # Best-effort: mark the matching executed_orders row cancelled. The
    # column is named alpaca_order_id for legacy reasons but holds whatever
    # broker order ID was stored at submission time (IBKR permId for IBKR).
    try:
        from backend.db.models import ExecutedOrder
        row = (
            db.query(ExecutedOrder)
            .filter(ExecutedOrder.alpaca_order_id == order_id)
            .first()
        )
        if row is not None:
            row.status = "cancelled"
            db.commit()
    except Exception as exc:
        logger.warning(
            "Cancel succeeded at broker but DB update failed for %s: %s",
            order_id, exc,
        )

    return result


# -- Order execution -----------------------------------------------------------
#
# Defence in depth — every layer must allow the order before it reaches the broker:
#   1. Pydantic schema  (in OrderConfirmRequest)
#   2. Live mode gate   (settings.is_live_mode → confirm_live_capital must be True)
#   3. Broker connected (no-op if client missing)
#   4. Daily order cap  (max N orders per 24h, prevents runaway)
#   5. Ticker whitelist (must be a position, open thesis, or watchlist ticker)
#   6. 20% rule         (order notional ≤ 20% of portfolio value)
# Every attempt — accepted OR rejected — gets a row in executed_orders.

ORDER_DAILY_CAP = 20
PORTFOLIO_MAX_PCT = 0.20


def _serialise_executed_order(o) -> dict:
    return {
        "id": o.id,
        "alpaca_order_id": o.alpaca_order_id,
        "ticker": o.ticker,
        "side": o.side,
        "qty": o.qty,
        "order_type": o.order_type,
        "order_class": o.order_class or "simple",
        "limit_price": o.limit_price,
        "stop_loss_price": o.stop_loss_price,
        "take_profit_price": o.take_profit_price,
        "target_qty": o.target_qty,
        "bracket_state": o.bracket_state,
        "fill_price": o.fill_price,
        "status": o.status,
        "rejection_reason": o.rejection_reason,
        "is_paper": o.is_paper,
        "rationale": o.rationale,
        "advisor_session_id": o.advisor_session_id,
        "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def _persist_rejection(db: Session, request: OrderConfirmRequest, reason: str) -> dict:
    """Record a rejected order attempt and return the serialised row."""
    rejected = crud.create_executed_order(
        db,
        ticker=request.ticker,
        side=request.side,
        qty=request.qty,
        order_type=request.order_type,
        limit_price=request.limit_price,
        status="rejected",
        rejection_reason=reason,
        is_paper=not settings.is_live_mode,
        rationale=request.rationale,
        advisor_session_id=request.advisor_session_id,
    )
    return _serialise_executed_order(rejected)


@app.post("/orders/confirm")
def confirm_order(
    request: OrderConfirmRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Execute a user-confirmed trade. Comes from the Execute button in the
    Advisor UI, never from the agent directly. Goes through every safety
    gate before touching Alpaca.
    """
    # Schema-level: limit_price required when order_type=limit
    if request.order_type == "limit" and request.limit_price is None:
        raise HTTPException(
            status_code=422,
            detail="limit_price is required when order_type is 'limit'",
        )
    if request.order_type == "market" and request.limit_price is not None:
        raise HTTPException(
            status_code=422,
            detail="limit_price must be null when order_type is 'market'",
        )

    # Bracket gate — BOTH stop_loss AND target_1 must be set (or neither).
    # A one-sided bracket is almost always a UI or advisor bug, and silently
    # treating it as a simple order would mask the missing leg. Refuse
    # explicitly and let the caller fix it.
    is_bracket = request.stop_loss is not None and request.target_1 is not None
    if (request.stop_loss is None) != (request.target_1 is None):
        raise HTTPException(
            status_code=422,
            detail=(
                "Bracket orders require BOTH stop_loss and target_1. "
                "Set both, or neither."
            ),
        )
    if is_bracket:
        # v1 constraints: long entries only, limit parent only, and the
        # levels have to be internally consistent. Each of these could be
        # relaxed later but only after the broker impls catch up.
        if request.side != "buy":
            raise HTTPException(
                status_code=422,
                detail="Bracket orders only support side='buy' in v1 (long entries).",
            )
        if request.order_type != "limit":
            raise HTTPException(
                status_code=422,
                detail=(
                    "Bracket orders require order_type='limit' so the parent "
                    "has a defined entry price. Use a limit entry."
                ),
            )
        if not (request.stop_loss < request.limit_price < request.target_1):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Bracket levels inconsistent: need "
                    f"stop_loss ({request.stop_loss}) < "
                    f"limit_price ({request.limit_price}) < "
                    f"target_1 ({request.target_1})."
                ),
            )
        # Scale-out validation: target_qty, when provided, must be a
        # positive integer strictly less than qty (if equal or greater,
        # it's a no-op — normalise to None). Integer check because
        # exchanges can't sell 1.5 shares at one price and 1.5 at another.
        if request.target_qty is not None:
            if request.target_qty >= request.qty:
                # Silent normalise — user asked for scale-out of the full
                # position, which is just a classic bracket. Clear the
                # field so downstream treats it as all-out.
                request.target_qty = None
            elif request.target_qty != int(request.target_qty):
                raise HTTPException(
                    status_code=422,
                    detail="target_qty must be a whole number of shares.",
                )
            elif int(request.qty) != request.qty:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Scale-out brackets require integer qty (the parent "
                        "qty and target_qty must both be whole shares so the "
                        "runner leg has a whole number of shares to cover)."
                    ),
                )

    # Gate 2 — live-mode confirmation
    if settings.is_live_mode and not request.confirm_live_capital:
        raise HTTPException(
            status_code=403,
            detail=(
                "System is in live mode. Set confirm_live_capital=true in the "
                "request body to acknowledge real capital is at risk."
            ),
        )

    # Gate 3 — broker must be connected
    client = get_broker()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Broker not configured. Start IB Gateway and set "
                "BROKER_PROVIDER=ibkr. This environment may be running with "
                "BROKER_PROVIDER=none (trading disabled by design, e.g. "
                "cloud-hosted analysis)."
            ),
        )

    # Gate 4 — daily order cap
    today_count = crud.count_orders_today(db)
    if today_count >= ORDER_DAILY_CAP:
        return _persist_rejection(
            db, request,
            f"Daily order cap reached ({today_count}/{ORDER_DAILY_CAP}). "
            "Wait 24h or raise ORDER_DAILY_CAP."
        )

    # Gate 5 — ticker whitelist (positions ∪ open theses ∪ watchlist)
    whitelist: set[str] = set()
    try:
        whitelist.update(
            (p.get("ticker") or "").upper() for p in client.get_positions()
        )
    except Exception as exc:
        logger.warning("Failed to load positions for whitelist: %s", exc)
    whitelist.update(
        (t.ticker or "").upper() for t in crud.get_open_theses(db, limit=100)
    )
    whitelist.update(crud.get_watchlist_tickers_set(db))

    if request.ticker not in whitelist:
        # Auto-whitelist trades that came from the Advisor UI flow.
        # By the time a request gets here with advisor_session_id AND a
        # rationale, the user has:
        #   1. Asked the advisor something, possibly via "Ask Advisor" deep
        #      link from Trade Ideas or a manual chat message
        #   2. Watched the agent emit a structured trade-proposal block
        #      after validating the ticker via market_data tool calls
        #   3. Clicked Review & execute on that specific card
        #   4. Typed ≥10 chars of their own thesis in the required textarea
        #   5. Clicked Execute
        # That chain is more than enough deliberate intent to justify adding
        # the ticker to the watchlist. Direct API calls without advisor
        # context still get rejected — the safety gate stays in place for
        # anything that bypasses the UI.
        if request.advisor_session_id and request.rationale:
            from datetime import date as _date
            crud.add_watchlist_ticker(
                db,
                ticker=request.ticker,
                notes=f"Auto-added from advisor trade on {_date.today().isoformat()}",
            )
            logger.info(
                "Auto-whitelisted %s from advisor session %s",
                request.ticker,
                request.advisor_session_id,
            )
            # Fall through to the remaining gates
        else:
            return _persist_rejection(
                db, request,
                f"Ticker {request.ticker} is not in the whitelist (positions, "
                "open theses, or watchlist). Add it to your watchlist first.",
            )

    # Gate 6 — 20% portfolio rule (use limit_price for limit orders, else fall
    # back to current quote via market_data; if neither, use a conservative
    # estimate from the user's account)
    try:
        account = client.get_account()
        portfolio_value = float(account.get("portfolio_value") or 0)
    except Exception as exc:
        logger.warning("Failed to load account for sizing check: %s", exc)
        portfolio_value = 0

    estimated_price = request.limit_price
    if estimated_price is None:
        # market order — fetch current quote
        from backend.tools.market_data import execute_market_data
        quote = execute_market_data(action="quote", ticker=request.ticker)
        if isinstance(quote, dict) and "price" in quote and quote["price"]:
            estimated_price = float(quote["price"])

    # The 20% rule is a position-SIZING cap — it's meant to prevent a
    # single BUY from blowing up concentration risk on the way in. Sells
    # of existing long shares reduce risk and should never be rejected
    # by this gate, even if the notional happens to be > 20% of equity
    # (common for 100% closes of big positions). In v1 we don't support
    # opening shorts via /orders/confirm, so "sell" always means
    # "closing a long" — safe to skip.
    if request.side == "buy" and estimated_price and portfolio_value:
        notional = estimated_price * request.qty
        max_notional = portfolio_value * PORTFOLIO_MAX_PCT
        if notional > max_notional:
            return _persist_rejection(
                db, request,
                f"Order notional ${notional:,.0f} exceeds 20% of portfolio "
                f"(${max_notional:,.0f}). Reduce qty or raise PORTFOLIO_MAX_PCT.",
            )

    # All gates passed — submit to the broker. Bracket path goes through
    # place_bracket_order which returns the parent leg; the attached
    # take-profit and stop-loss legs live at the broker from here on.
    from datetime import datetime, timezone

    # Classify: simple / bracket / scale-out. Scale-out is a subtype of
    # bracket with target_qty < qty. Kept as a separate column value so
    # the position-poll state machine can find rows to react to without
    # re-parsing target_qty. None target_qty or equal-to-qty = plain bracket.
    is_scale_out = (
        is_bracket
        and request.target_qty is not None
        and request.target_qty < request.qty
    )
    resolved_order_class = (
        "scale_out" if is_scale_out else ("bracket" if is_bracket else "simple")
    )
    initial_bracket_state = "fresh" if is_scale_out else None

    try:
        if is_bracket:
            result = client.place_bracket_order(
                ticker=request.ticker,
                qty=request.qty,
                side=request.side,
                limit_price=request.limit_price,
                stop_loss_price=request.stop_loss,
                take_profit_price=request.target_1,
                paper_only=not settings.is_live_mode,
                target_qty=request.target_qty,
            )
        else:
            result = client.place_order(
                ticker=request.ticker,
                qty=request.qty,
                side=request.side,
                paper_only=not settings.is_live_mode,
                order_type=request.order_type,
                limit_price=request.limit_price,
            )
    except Exception as exc:
        logger.exception("Broker order placement failed for %s", request.ticker)
        failed = crud.create_executed_order(
            db,
            ticker=request.ticker,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            order_class=resolved_order_class,
            limit_price=request.limit_price,
            stop_loss_price=request.stop_loss,
            take_profit_price=request.target_1,
            target_qty=request.target_qty,
            bracket_state=initial_bracket_state,
            status="failed",
            rejection_reason=str(exc),
            is_paper=not settings.is_live_mode,
            rationale=request.rationale,
            advisor_session_id=request.advisor_session_id,
        )
        return _serialise_executed_order(failed)

    persisted = crud.create_executed_order(
        db,
        alpaca_order_id=result.order_id,
        ticker=result.ticker,
        side=result.side,
        qty=result.qty,
        order_type=request.order_type,
        order_class=resolved_order_class,
        limit_price=request.limit_price,
        stop_loss_price=request.stop_loss,
        take_profit_price=request.target_1,
        target_qty=request.target_qty,
        bracket_state=initial_bracket_state,
        fill_price=result.fill_price,
        status="filled" if result.fill_price else "accepted",
        is_paper=result.is_paper,
        rationale=request.rationale,
        advisor_session_id=request.advisor_session_id,
        submitted_at=datetime.now(timezone.utc),
        filled_at=datetime.now(timezone.utc) if result.fill_price else None,
    )

    # Create a trade journal entry alongside the executed order.
    # This is the "discipline" capture: the user's thesis was required on
    # the request, and we persist it now so the journal page can prompt
    # for a lesson when the position closes.
    #
    # Only buys open new journal entries — sells are usually closes.
    # Closes are detected by the position polling job, which finds the
    # matching open journal entry and fills in the close fields.
    if request.side == "buy":
        # Use limit_price for limit orders, fill_price for market orders
        open_price = result.fill_price or request.limit_price or 0.0
        crud.create_journal_entry(
            db,
            open_executed_order_id=persisted.id,
            ticker=result.ticker,
            side=request.side,
            qty=result.qty,
            open_price=float(open_price),
            opened_at=datetime.now(timezone.utc),
            user_thesis=request.user_thesis,
            user_disagreement=request.user_disagreement,
            advisor_session_id=request.advisor_session_id,
            advisor_rationale=request.rationale,
            status="open",
        )

    return _serialise_executed_order(persisted)


def _serialise_journal_entry(e) -> dict:
    needs_thesis = not e.user_thesis or e.user_thesis.strip() == ""
    needs_lesson = e.status == "closed" and (
        not e.user_lesson or e.user_lesson.strip() == ""
    )
    return {
        "id": e.id,
        "open_executed_order_id": e.open_executed_order_id,
        "close_executed_order_id": e.close_executed_order_id,
        "ticker": e.ticker,
        "side": e.side,
        "qty": e.qty,
        "open_price": e.open_price,
        "opened_at": e.opened_at.isoformat() if e.opened_at else None,
        "user_thesis": e.user_thesis,
        "user_disagreement": e.user_disagreement,
        "advisor_session_id": e.advisor_session_id,
        "advisor_rationale": e.advisor_rationale,
        "close_price": e.close_price,
        "closed_at": e.closed_at.isoformat() if e.closed_at else None,
        "pnl_amount": e.pnl_amount,
        "pnl_pct": e.pnl_pct,
        "days_held": e.days_held,
        "advised_direction_profitable": e.advised_direction_profitable,
        "user_lesson": e.user_lesson,
        "status": e.status,
        "needs_thesis": needs_thesis,
        "needs_lesson": needs_lesson,
        "needs_action": needs_thesis or needs_lesson,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


class JournalThesisRequest(BaseModel):
    user_thesis: str = Field(min_length=10, max_length=500)
    user_disagreement: Optional[str] = Field(default=None, max_length=500)


class JournalLessonRequest(BaseModel):
    user_lesson: str = Field(min_length=10, max_length=1000)


@app.get("/journal")
def list_journal(
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    List journal entries. Optional status filter (open|closed).
    Always returns newest-first.
    """
    if status is not None and status not in ("open", "closed"):
        raise HTTPException(status_code=422, detail="Invalid status filter")
    entries = crud.list_journal_entries(
        db, status=status, limit=min(max(1, limit), 500)
    )
    return [_serialise_journal_entry(e) for e in entries]


@app.get("/journal/action-needed-count")
def journal_action_needed_count(
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Lightweight count for the nav badge — polled by the UI."""
    return {"count": crud.count_journal_entries_needing_action(db)}


@app.get("/journal/{entry_id}")
def get_journal_entry_route(
    entry_id: str,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    entry = crud.get_journal_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return _serialise_journal_entry(entry)


@app.post("/journal/{entry_id}/thesis")
def add_journal_thesis(
    entry_id: str,
    request: JournalThesisRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Backfill the thesis on a journal entry that was created without one
    (i.e., a manual trade detected by the polling job).
    """
    entry = crud.get_journal_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    if entry.user_thesis and entry.user_thesis.strip():
        raise HTTPException(
            status_code=409,
            detail="This entry already has a thesis. Theses are immutable.",
        )
    updated = crud.update_journal_entry(
        db,
        entry_id,
        user_thesis=request.user_thesis,
        user_disagreement=request.user_disagreement,
    )
    return _serialise_journal_entry(updated)


@app.post("/journal/{entry_id}/lesson")
def add_journal_lesson(
    entry_id: str,
    request: JournalLessonRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Add the lesson learned to a closed journal entry. Only valid after
    the position has closed (status=closed). Lessons are immutable once set.
    """
    entry = crud.get_journal_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    if entry.status != "closed":
        raise HTTPException(
            status_code=409,
            detail="Lesson can only be added after the position closes.",
        )
    if entry.user_lesson and entry.user_lesson.strip():
        raise HTTPException(
            status_code=409,
            detail="This entry already has a lesson. Lessons are immutable.",
        )
    updated = crud.update_journal_entry(
        db, entry_id, user_lesson=request.user_lesson
    )
    return _serialise_journal_entry(updated)


@app.get("/orders/executed")
def list_executed_orders(
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """List orders MarketCoach placed, newest first. Optional status filter."""
    if status is not None and status not in (
        "accepted", "filled", "rejected", "failed"
    ):
        raise HTTPException(status_code=422, detail="Invalid status filter")
    orders = crud.list_executed_orders(
        db, status=status, limit=min(max(1, limit), 200)
    )
    return [_serialise_executed_order(o) for o in orders]


# -- Trade ideas ---------------------------------------------------------------

@app.get("/trade-ideas")
def get_trade_ideas(
    active_only: bool = True,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return trade ideas (active by default, or all)."""
    if active_only:
        ideas = crud.get_active_trade_ideas(db, limit=limit)
    else:
        ideas = crud.get_trade_ideas_page(db, skip=skip, limit=limit)

    return [
        {
            "id": i.id,
            "thesis_id": i.thesis_id,
            "ticker": i.ticker,
            "direction": i.direction,
            "strategy": i.strategy,
            "horizon": i.horizon,
            "entry_price": i.entry_price,
            "stop_loss": i.stop_loss,
            "take_profit": i.take_profit,
            "current_price": i.current_price,
            "confidence": i.confidence,
            "risk_reward_ratio": i.risk_reward_ratio,
            "position_size_pct": i.position_size_pct,
            "max_loss_pct": i.max_loss_pct,
            "rationale": i.rationale,
            "key_levels": json.loads(i.key_levels) if i.key_levels else {},
            "status": i.status,
            "created_at": i.created_at.isoformat(),
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
        }
        for i in ideas
    ]


@app.patch("/trade-ideas/{idea_id}")
def update_trade_idea(
    idea_id: str,
    status: str = Query(pattern=r"^(executed|cancelled)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Mark a trade idea as executed or cancelled."""
    from datetime import datetime, timezone
    executed_at = datetime.now(timezone.utc) if status == "executed" else None
    idea = crud.update_trade_idea_status(db, idea_id, status, executed_at)
    if idea is None:
        raise HTTPException(status_code=404, detail="Trade idea not found")
    return {"id": idea.id, "status": idea.status}


# -- User profile / memory ----------------------------------------------------

@app.get("/profile")
def get_profile(db: Session = Depends(get_db)):
    """Return stored user preferences and profile memories."""
    memories = crud.get_all_memories(db)
    return {
        "memories": [
            {
                "key": m.key,
                "value": m.value,
                "category": m.category,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
            for m in memories
        ]
    }


@app.post("/profile")
def set_profile_memory(
    request: MemoryRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """Manually set a user preference or profile memory."""
    memory = crud.upsert_memory(
        db, key=request.key, value=request.value, category=request.category,
    )
    return {
        "key": memory.key,
        "value": memory.value,
        "category": memory.category,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
    }


# -- Market data ---------------------------------------------------------------
#
# HTTP surface on top of backend.tools.market_data's yfinance functions.
# Used by the Markets page in the frontend — clickable ticker list + chart.
# Kept as thin wrappers over _action_quote / _action_price_history rather
# than duplicating yfinance logic. Errors from market_data bubble up as
# 404 / 502 so the frontend can show a clean message instead of a spinner.

@app.get("/market-data/{ticker}/quote")
def get_market_quote(
    ticker: str,
    _auth: str = Depends(require_auth),
):
    """Current price, change, volume, market cap for a single ticker."""
    from backend.tools.market_data import execute_market_data
    result = execute_market_data("quote", ticker=ticker)
    if "error" in result:
        raise HTTPException(
            status_code=404 if "No data" in result["error"] else 502,
            detail=result["error"],
        )
    return result


@app.get("/market-data/{ticker}/history")
def get_market_history(
    ticker: str,
    days: int = Query(default=30, ge=1, le=365),
    _auth: str = Depends(require_auth),
):
    """
    Daily OHLCV history for charting. days=7 for ~1W, 30 for 1M, 90 for 3M,
    180 for 6M, 365 for 1Y. Frontend picks the number based on the period
    button the user clicked.
    """
    from backend.tools.market_data import execute_market_data
    result = execute_market_data("price_history", ticker=ticker, days=days)
    if "error" in result:
        raise HTTPException(
            status_code=404 if "No price history" in result["error"] else 502,
            detail=result["error"],
        )
    return result


# -- Backtest ------------------------------------------------------------------

@app.post("/backtest/run")
def run_backtest_endpoint(
    request: BacktestRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_auth),
):
    """
    Run a time-machine backtest simulation.

    This calls Claude at each decision point to generate theses from
    historical technicals, then simulates the trades day-by-day.
    May take 1-3 minutes depending on the date range and watchlist size.
    """
    from datetime import date, datetime, timedelta, timezone
    from backend.backtest.schemas import BacktestConfig
    from backend.backtest.engine import run_backtest

    start = date.fromisoformat(request.start_date)
    end = date.fromisoformat(request.end_date) if request.end_date else date.today() - timedelta(days=1)
    # Backtest watchlist: request body override first, DB watchlist second,
    # env default last. The last fallback should never trigger in practice
    # because the lifespan seeds the DB on first startup.
    db_watchlist = [row.ticker for row in crud.list_watchlist_tickers(db)]
    watchlist = [
        t.strip().upper()
        for t in (request.watchlist or db_watchlist or settings.watchlist)
    ]

    if start >= end:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")
    if (end - start).days > 365:
        raise HTTPException(status_code=400, detail="Maximum backtest window is 365 days")
    if (end - start).days < 7:
        raise HTTPException(status_code=400, detail="Minimum backtest window is 7 days")

    config = BacktestConfig(
        start_date=start,
        end_date=end,
        initial_capital=request.initial_capital,
        watchlist=watchlist,
        decision_interval_days=request.decision_interval_days,
        risk_tolerance=request.risk_tolerance,
    )

    try:
        result = run_backtest(config)
    except Exception as exc:
        logger.exception("Backtest failed")
        raise HTTPException(status_code=500, detail=f"Backtest failed: {exc}")

    # Persist to DB
    crud.create_backtest_run(
        db,
        id=result.id,
        config_json=result.config.model_dump_json(),
        result_json=result.model_dump_json(),
        status=result.status,
        initial_capital=result.metrics.initial_capital,
        final_equity=result.metrics.final_equity,
        total_return_pct=result.metrics.total_return_pct,
        total_trades=result.metrics.total_trades,
        win_rate_pct=result.metrics.win_rate_pct,
        max_drawdown_pct=result.metrics.max_drawdown_pct,
        started_at=datetime.fromisoformat(result.started_at),
        completed_at=datetime.fromisoformat(result.completed_at),
    )

    return result.model_dump()


@app.get("/backtests")
def list_backtests(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """List past backtest runs (summary only, not full results)."""
    runs = crud.get_backtest_runs(db, skip=skip, limit=limit)
    return [
        {
            "id": r.id,
            "status": r.status,
            "initial_capital": r.initial_capital,
            "final_equity": r.final_equity,
            "total_return_pct": r.total_return_pct,
            "total_trades": r.total_trades,
            "win_rate_pct": r.win_rate_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs
    ]


@app.get("/backtest/{run_id}")
def get_backtest(run_id: str, db: Session = Depends(get_db)):
    """Fetch full backtest results by ID."""
    run = crud.get_backtest_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return json.loads(run.result_json)


# -- Market calendar -----------------------------------------------------------

# FOMC scheduled rate-decision dates (second day of each meeting).
# Update this list each year when the Fed publishes its schedule.
_FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


@app.get("/events")
def get_events(
    year: int = Query(default=None, ge=2020, le=2030),
    month: int = Query(default=None, ge=1, le=12),
    db: Session = Depends(get_db),
):
    """
    Return market events for a given month: upcoming earnings dates for all
    watchlist tickers (sourced from yfinance) and FOMC rate-decision dates.
    Defaults to the current month when year/month are omitted.
    """
    import calendar as cal_lib
    from datetime import date

    import pandas as pd
    import yfinance as yf

    today = date.today()
    y = year if year is not None else today.year
    m = month if month is not None else today.month

    _, days_in_month = cal_lib.monthrange(y, m)
    start = date(y, m, 1)
    end = date(y, m, days_in_month)

    events: list[dict] = []

    # -- FOMC dates (extend list above for years beyond 2026) --
    for d_str in _FOMC_DATES_2026:
        d = date.fromisoformat(d_str)
        if start <= d <= end:
            events.append({
                "date": d_str,
                "type": "fomc",
                "ticker": None,
                "title": "FOMC Rate Decision",
                "detail": "Federal Reserve interest rate decision",
            })

    # -- Earnings dates from yfinance for each watchlist ticker --
    # Read from DB (the live watchlist); fall back to env default if empty.
    _watchlist = [row.ticker for row in crud.list_watchlist_tickers(db)] or settings.watchlist
    for ticker in _watchlist:
        try:
            t = yf.Ticker(ticker)
            earn_df = t.earnings_dates
            if earn_df is None or earn_df.empty:
                continue

            # Identify the "Reported EPS" column to skip already-reported dates.
            reported_col = next(
                (c for c in earn_df.columns if "Reported" in c), None
            )

            for idx in earn_df.index:
                try:
                    # Skip past earnings that have already been reported.
                    if reported_col is not None and not pd.isna(earn_df.loc[idx, reported_col]):
                        continue
                    d = idx.date() if hasattr(idx, "date") else pd.Timestamp(idx).date()
                    if start <= d <= end:
                        events.append({
                            "date": d.isoformat(),
                            "type": "earnings",
                            "ticker": ticker,
                            "title": f"{ticker} Earnings",
                            "detail": None,
                        })
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Failed to fetch earnings for %s: %s", ticker, exc)
            continue

    events.sort(key=lambda x: x["date"])
    return events
