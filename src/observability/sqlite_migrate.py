"""Additive column migrations for the observability SQLite stores.

Each store opens its DB with ``CREATE TABLE IF NOT EXISTS`` and then adds any
column that an older DB predates. That step used to wrap every ``ALTER`` in a
bare ``except sqlite3.OperationalError``, which also swallowed a lock timeout
or a full disk: the store started without the column and every later insert
failed until the process restarted.

:func:`add_missing_columns` reads the table's columns once and only ALTERs the
ones that are missing. A failed ALTER raises, except for "duplicate column
name", which means a concurrent opener added the same column first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def add_missing_columns(db: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]]) -> list[str]:
    """ALTER ``table`` to add each ``(name, type_decl)`` column it lacks.

    Returns the names of the columns this call added. Raises the
    ``sqlite3.OperationalError`` from any ALTER that fails for a reason other
    than the column already existing.
    """
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    added: list[str] = []
    for name, type_decl in columns:
        if name in present:
            continue
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {type_decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            # Another connection added it between our PRAGMA and this ALTER.
        else:
            added.append(name)
        present.add(name)
    return added
