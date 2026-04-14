"""
CRUD helpers for each model.
All functions accept a SQLAlchemy Session and return ORM objects or lists.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.db.models import (
    AutoRule,
    BacktestRun,
    ChatMessage,
    ExecutedOrder,
    MorningBrief,
    NewsReaction,
    Position,
    Signal,
    Thesis,
    TradeIdea,
    UserMemory,
    WeeklyPlan,
)


# -- Signals ------------------------------------------------------------------

def create_signal(db: Session, **kwargs) -> Signal:
    signal = Signal(**kwargs)
    db.add(signal)
    db.commit()
    db.refresh(signal)
    return signal


def create_signals_batch(db: Session, signals_data: list[dict]) -> int:
    """Insert multiple signals in a single transaction. Returns count inserted."""
    objects = [Signal(**s) for s in signals_data]
    db.add_all(objects)
    db.commit()
    return len(objects)


def get_recent_signals(
    db: Session,
    hours: int = 4,
    ticker: Optional[str] = None,
) -> list[Signal]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = db.query(Signal).filter(Signal.created_at >= cutoff)
    if ticker:
        q = q.filter(Signal.ticker == ticker)
    return q.order_by(Signal.created_at.desc()).all()


def get_signals_page(db: Session, skip: int = 0, limit: int = 50) -> list[Signal]:
    return db.query(Signal).order_by(Signal.created_at.desc()).offset(skip).limit(limit).all()


# -- Theses --------------------------------------------------------------------

def create_thesis(db: Session, **kwargs) -> Thesis:
    thesis = Thesis(**kwargs)
    db.add(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


def create_theses_batch(db: Session, theses_data: list[dict]) -> int:
    """Insert multiple theses in a single transaction. Returns count inserted."""
    objects = [Thesis(**t) for t in theses_data]
    db.add_all(objects)
    db.commit()
    return len(objects)


def get_open_theses(db: Session, limit: int = 200) -> list[Thesis]:
    return (
        db.query(Thesis)
        .filter(Thesis.resolved_at.is_(None))
        .order_by(Thesis.created_at.desc())
        .limit(limit)
        .all()
    )


def get_theses_page(db: Session, skip: int = 0, limit: int = 50) -> list[Thesis]:
    return db.query(Thesis).order_by(Thesis.created_at.desc()).offset(skip).limit(limit).all()


def resolve_thesis(
    db: Session,
    thesis_id: str,
    outcome: str,
    accuracy: float,
) -> Optional[Thesis]:
    thesis = db.query(Thesis).filter(Thesis.id == thesis_id).first()
    if thesis is None:
        return None
    thesis.resolved_at = datetime.now(timezone.utc)
    thesis.outcome = outcome
    thesis.accuracy = accuracy
    db.commit()
    db.refresh(thesis)
    return thesis


def get_accuracy_stats(db: Session) -> dict:
    """Return thesis accuracy stats using SQL aggregates (no full table load)."""
    row = (
        db.query(
            func.count(Thesis.id).label("total"),
            func.sum(case((Thesis.outcome == "correct", 1), else_=0)).label("correct"),
            func.sum(case((Thesis.outcome == "incorrect", 1), else_=0)).label("incorrect"),
        )
        .filter(Thesis.resolved_at.isnot(None))
        .one()
    )

    total = row.total or 0
    correct = row.correct or 0
    incorrect = row.incorrect or 0

    return {
        "total": total,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy_pct": round(correct / total * 100, 1) if total > 0 else None,
    }


# -- Trade ideas ---------------------------------------------------------------

def create_trade_idea(db: Session, **kwargs) -> TradeIdea:
    idea = TradeIdea(**kwargs)
    db.add(idea)
    db.commit()
    db.refresh(idea)
    return idea


def create_trade_ideas_batch(db: Session, ideas_data: list[dict]) -> int:
    """Insert multiple trade ideas in a single transaction. Returns count inserted."""
    objects = [TradeIdea(**d) for d in ideas_data]
    db.add_all(objects)
    db.commit()
    return len(objects)


def get_active_trade_ideas(db: Session, limit: int = 20) -> list[TradeIdea]:
    """Return active trade ideas ordered by confidence descending."""
    return (
        db.query(TradeIdea)
        .filter(TradeIdea.status == "active")
        .order_by(TradeIdea.confidence.desc())
        .limit(limit)
        .all()
    )


def get_trade_ideas_for_ticker(db: Session, ticker: str, limit: int = 10) -> list[TradeIdea]:
    """Return trade ideas for a specific ticker, newest first."""
    return (
        db.query(TradeIdea)
        .filter(TradeIdea.ticker == ticker)
        .order_by(TradeIdea.created_at.desc())
        .limit(limit)
        .all()
    )


def update_trade_idea_status(
    db: Session,
    idea_id: str,
    status: str,
    executed_at: Optional[datetime] = None,
) -> Optional[TradeIdea]:
    """Update status of a trade idea. Returns None if not found."""
    idea = db.query(TradeIdea).filter(TradeIdea.id == idea_id).first()
    if idea is None:
        return None
    idea.status = status
    if executed_at is not None:
        idea.executed_at = executed_at
    db.commit()
    db.refresh(idea)
    return idea


def get_trade_ideas_page(db: Session, skip: int = 0, limit: int = 50) -> list[TradeIdea]:
    """Return paginated trade ideas across all statuses, newest first."""
    return (
        db.query(TradeIdea)
        .order_by(TradeIdea.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def expire_stale_trade_ideas(db: Session) -> int:
    """Set status='expired' for active ideas past their expires_at. Returns count updated."""
    now = datetime.now(timezone.utc)
    count = (
        db.query(TradeIdea)
        .filter(
            TradeIdea.status == "active",
            TradeIdea.expires_at.isnot(None),
            TradeIdea.expires_at < now,
        )
        .update({"status": "expired"}, synchronize_session="fetch")
    )
    db.commit()
    return count


# -- Chat messages -------------------------------------------------------------

def create_message(db: Session, session_id: str, role: str, content: str) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role=role, content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def get_session_messages(db: Session, session_id: str) -> list[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
        .all()
    )


# -- Positions -----------------------------------------------------------------

def upsert_position(db: Session, **kwargs) -> Position:
    position = Position(**kwargs)
    db.add(position)
    db.commit()
    db.refresh(position)
    return position


def get_open_positions(db: Session, paper_only: bool = True) -> list[Position]:
    q = db.query(Position)
    if paper_only:
        q = q.filter(Position.is_paper.is_(True))
    return q.all()


# -- AutoRules -----------------------------------------------------------------

def get_active_rules(db: Session) -> list[AutoRule]:
    return db.query(AutoRule).filter(AutoRule.active.is_(True)).all()


# -- User memories -------------------------------------------------------------

def upsert_memory(
    db: Session,
    key: str,
    value: str,
    category: str = "general",
) -> UserMemory:
    """Create or update a user memory by key."""
    existing = db.query(UserMemory).filter(UserMemory.key == key).first()
    if existing:
        existing.value = value
        existing.category = category
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(existing)
        return existing
    memory = UserMemory(key=key, value=value, category=category)
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


def get_memory(db: Session, key: str) -> Optional[UserMemory]:
    """Return a single memory by key, or None."""
    return db.query(UserMemory).filter(UserMemory.key == key).first()


def get_memories_by_category(db: Session, category: str) -> list[UserMemory]:
    """Return all memories in a given category."""
    return (
        db.query(UserMemory)
        .filter(UserMemory.category == category)
        .order_by(UserMemory.updated_at.desc())
        .all()
    )


def get_all_memories(db: Session) -> list[UserMemory]:
    """Return every stored user memory."""
    return db.query(UserMemory).order_by(UserMemory.updated_at.desc()).all()


def delete_memory(db: Session, key: str) -> bool:
    """Delete a memory by key. Returns True if found and deleted, False otherwise."""
    existing = db.query(UserMemory).filter(UserMemory.key == key).first()
    if existing is None:
        return False
    db.delete(existing)
    db.commit()
    return True


# -- Backtest runs -------------------------------------------------------------

def create_backtest_run(db: Session, **kwargs) -> BacktestRun:
    run = BacktestRun(**kwargs)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_backtest_run(db: Session, run_id: str) -> Optional[BacktestRun]:
    return db.query(BacktestRun).filter(BacktestRun.id == run_id).first()


def get_backtest_runs(db: Session, skip: int = 0, limit: int = 20) -> list[BacktestRun]:
    return (
        db.query(BacktestRun)
        .order_by(BacktestRun.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


# -- News reactions ------------------------------------------------------------

def create_news_reaction(db: Session, **kwargs) -> NewsReaction:
    reaction = NewsReaction(**kwargs)
    db.add(reaction)
    db.commit()
    db.refresh(reaction)
    return reaction


def list_news_reactions(
    db: Session,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[NewsReaction]:
    q = db.query(NewsReaction)
    if status is not None:
        q = q.filter(NewsReaction.status == status)
    return q.order_by(NewsReaction.created_at.desc()).limit(limit).all()


def get_news_reaction(db: Session, reaction_id: str) -> Optional[NewsReaction]:
    return db.query(NewsReaction).filter(NewsReaction.id == reaction_id).first()


def count_unread_reactions(db: Session) -> int:
    return (
        db.query(func.count(NewsReaction.id))
        .filter(NewsReaction.status == "unread")
        .scalar()
        or 0
    )


def update_reaction_status(
    db: Session, reaction_id: str, status: str
) -> Optional[NewsReaction]:
    reaction = db.query(NewsReaction).filter(NewsReaction.id == reaction_id).first()
    if reaction is None:
        return None
    reaction.status = status
    db.commit()
    db.refresh(reaction)
    return reaction


def has_recent_reaction_for_ticker(
    db: Session, ticker: str, hours: int
) -> bool:
    """True if a reaction for this ticker was created within the dedupe window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return (
        db.query(NewsReaction.id)
        .filter(NewsReaction.ticker == ticker)
        .filter(NewsReaction.created_at >= cutoff)
        .first()
        is not None
    )


# -- Weekly plans --------------------------------------------------------------

def create_weekly_plan(db: Session, **kwargs) -> WeeklyPlan:
    plan = WeeklyPlan(**kwargs)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def get_latest_weekly_plan(db: Session) -> Optional[WeeklyPlan]:
    """Return the most recently generated weekly plan, or None if none exist."""
    return (
        db.query(WeeklyPlan)
        .order_by(WeeklyPlan.created_at.desc())
        .first()
    )


def list_weekly_plans(db: Session, limit: int = 10) -> list[WeeklyPlan]:
    return (
        db.query(WeeklyPlan)
        .order_by(WeeklyPlan.created_at.desc())
        .limit(limit)
        .all()
    )


# -- Executed orders -----------------------------------------------------------

def create_executed_order(db: Session, **kwargs) -> ExecutedOrder:
    order = ExecutedOrder(**kwargs)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def list_executed_orders(
    db: Session,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[ExecutedOrder]:
    q = db.query(ExecutedOrder)
    if status is not None:
        q = q.filter(ExecutedOrder.status == status)
    return q.order_by(ExecutedOrder.created_at.desc()).limit(limit).all()


def count_orders_today(db: Session) -> int:
    """Number of order attempts (any status) created in the last 24 hours.
    Used by the per-day cap to prevent runaway from a stuck loop."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return (
        db.query(func.count(ExecutedOrder.id))
        .filter(ExecutedOrder.created_at >= cutoff)
        .scalar()
        or 0
    )


# -- Morning briefs ------------------------------------------------------------

def create_morning_brief(db: Session, **kwargs) -> MorningBrief:
    brief = MorningBrief(**kwargs)
    db.add(brief)
    db.commit()
    db.refresh(brief)
    return brief


def get_latest_morning_brief(db: Session) -> Optional[MorningBrief]:
    return (
        db.query(MorningBrief)
        .order_by(MorningBrief.created_at.desc())
        .first()
    )


def list_morning_briefs(db: Session, limit: int = 14) -> list[MorningBrief]:
    return (
        db.query(MorningBrief)
        .order_by(MorningBrief.created_at.desc())
        .limit(limit)
        .all()
    )
