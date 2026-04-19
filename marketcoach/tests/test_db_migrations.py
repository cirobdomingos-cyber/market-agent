"""
Tests for the lightweight additive migration helper in backend.db.

The helper (_apply_additive_migrations) exists so long-lived databases
— the user's local marketcoach.db and the Railway volume DB — gain
new columns when the model evolves, without needing Alembic. These
tests verify the helper is idempotent and correctly skips columns
that already exist.
"""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base


def _fresh_engine():
    return create_engine("sqlite:///:memory:")


def _existing_cols(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


class TestAdditiveMigrations:
    def test_is_idempotent_on_fresh_db(self, monkeypatch):
        """
        create_all writes every current column. The migration loop should
        then detect each target column as already present and no-op.
        """
        from backend.db import _apply_additive_migrations
        import backend.db as db_module

        engine = _fresh_engine()
        monkeypatch.setattr(db_module, "engine", engine)
        Base.metadata.create_all(bind=engine)

        before = _existing_cols(engine, "executed_orders")
        _apply_additive_migrations()
        after = _existing_cols(engine, "executed_orders")
        assert before == after

    def test_adds_missing_column_on_legacy_db(self, monkeypatch):
        """
        Simulate a legacy DB that pre-dates target_qty / bracket_state:
        start with a stripped executed_orders table, then verify the
        helper adds the missing columns.
        """
        from backend.db import _apply_additive_migrations
        import backend.db as db_module

        engine = _fresh_engine()
        monkeypatch.setattr(db_module, "engine", engine)

        # Minimal executed_orders table — just the columns that existed
        # before any of the additive migrations we care about.
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE executed_orders (
                    id VARCHAR PRIMARY KEY,
                    ticker VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    qty FLOAT NOT NULL,
                    order_type VARCHAR NOT NULL,
                    status VARCHAR NOT NULL
                )
            """))

        before = _existing_cols(engine, "executed_orders")
        assert "target_qty" not in before
        assert "bracket_state" not in before

        _apply_additive_migrations()

        after = _existing_cols(engine, "executed_orders")
        assert "target_qty" in after
        assert "bracket_state" in after
        assert "order_class" in after  # earlier migration still applies

    def test_skips_silently_when_table_missing(self, monkeypatch):
        """
        If the table doesn't exist at all (truly fresh deploy before
        create_all runs), the migration loop should no-op without raising.
        Defensive — create_all happens first in init_db, but the loop
        should still be robust to call-order surprises.
        """
        from backend.db import _apply_additive_migrations
        import backend.db as db_module

        engine = _fresh_engine()
        monkeypatch.setattr(db_module, "engine", engine)

        # No tables created at all
        assert inspect(engine).get_table_names() == []
        _apply_additive_migrations()  # must not raise
        # Still no tables
        assert inspect(engine).get_table_names() == []

    def test_init_db_creates_schema_and_runs_migrations(self, monkeypatch):
        """End-to-end: init_db on a fresh engine leaves every model column present."""
        from backend.db import init_db
        import backend.db as db_module

        engine = _fresh_engine()
        monkeypatch.setattr(db_module, "engine", engine)

        init_db()

        cols = _existing_cols(engine, "executed_orders")
        for expected in (
            "target_qty",
            "bracket_state",
            "order_class",
            "stop_loss_price",
            "take_profit_price",
        ):
            assert expected in cols, f"missing column: {expected}"
