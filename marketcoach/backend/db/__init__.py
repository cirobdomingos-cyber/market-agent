import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from typing import Generator

from backend.config import settings
from backend.db.models import Base

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    # SQLite-specific: allow use from multiple threads (FastAPI uses a thread pool)
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Lightweight additive migrations. One entry per (table, column) added
# after a prior release — read the model definition for the current type.
# ADDITIVE ONLY: this loop never drops, renames, or changes types. If we
# ever need something heavier, switch to Alembic.
_MIGRATIONS = [
    ("executed_orders", "order_class", "VARCHAR"),
    ("executed_orders", "stop_loss_price", "FLOAT"),
    ("executed_orders", "take_profit_price", "FLOAT"),
    ("executed_orders", "target_qty", "FLOAT"),
    ("executed_orders", "bracket_state", "VARCHAR"),
]


def _apply_additive_migrations() -> None:
    """
    Add columns to existing tables if they're missing. Idempotent — safe
    to run on every startup. Skips silently when the table doesn't exist
    yet (create_all will create it from scratch with all columns present).

    Why this exists: SQLAlchemy's create_all only creates NEW tables; it
    never ALTERs existing ones. Without this helper, any developer with a
    long-lived marketcoach.db (including the Railway volume DB) would hit
    OperationalError the moment new code INSERTs into a column their old
    table schema doesn't know about — the bug that broke the first live
    trade on 2026-04-17.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, coltype in _MIGRATIONS:
            if table not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_cols:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                logger.info(
                    "Schema migration: ALTER TABLE %s ADD COLUMN %s %s",
                    table, column, coltype,
                )
            except Exception as exc:
                # Non-fatal: if another worker raced us to the ALTER, we
                # just log and move on. Next startup sees the column and
                # skips. Never crash boot on a migration we can retry.
                logger.warning(
                    "Schema migration skipped (%s.%s): %s",
                    table, column, exc,
                )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
