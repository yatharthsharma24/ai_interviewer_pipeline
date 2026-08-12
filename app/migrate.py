"""A deliberately minimal stand-in for Alembic, shared by both databases.

``create_all`` only creates missing *tables*. A database file made before a column was added
silently lacks that column, and then every query touching it fails with "no such column" —
which is what happens to anyone who has been running the pipeline and pulls a new version.

Two operations, and only two: **add** a column that the models have and the file does not,
and **rename** one that has been renamed in the models. No drops, no type changes, no data
backfill. Anything past that needs a real migration tool, and pretending otherwise here
would be worse than the honest limitation.

Both are idempotent, so ``init_db`` can call them on every start.
"""

from __future__ import annotations

import logging

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

RENAMED_COLUMNS: dict[str, list[tuple[str, str]]] = {}


def rename_columns(engine: Engine) -> list[str]:
    """Apply ``RENAMED_COLUMNS`` to an existing SQLite file. Returns what it changed."""
    if engine.dialect.name != "sqlite":
        return []

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    applied: list[str] = []

    with engine.begin() as connection:
        for table, renames in RENAMED_COLUMNS.items():
            if table not in existing:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for old, new in renames:
                if old in present and new not in present:
                    connection.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
                    present.discard(old)
                    present.add(new)
                    applied.append(f"{table}.{old} -> {new}")

    for change in applied:
        logger.info("Renamed column %s", change)
    return applied


def ensure_columns(engine: Engine, metadata: MetaData) -> list[str]:
    """Add columns present in ``metadata`` but missing from the file. Returns what it added."""
    if engine.dialect.name != "sqlite":
        return []

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if table.name not in existing:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} ADD COLUMN "
                    f"{column.name} {column.type.compile(engine.dialect)}"
                )
                if column.default is not None and getattr(column.default, "is_scalar", False):
                    value = column.default.arg
                    literal = f"'{value}'" if isinstance(value, str) else value
                    ddl += f" DEFAULT {literal}"
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")

    for change in added:
        logger.info("Added column %s", change)
    return added


def migrate(engine: Engine, metadata: MetaData) -> list[str]:
    """Renames first, then additions.

    Order matters: run the other way round, the add step would see the new name missing,
    create it empty, and leave the rename with nowhere to move its data.
    """
    return rename_columns(engine) + ensure_columns(engine, metadata)
