"""Forward-only SQLite schema migrations.

Migrations only move forward. ``schema_migrations(version, checksum,
applied_at)`` records what ran; if the content of an already-applied migration
changes, we refuse to start instead of silently diverging.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..time import utc_now

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def available_migrations() -> list[tuple[str, str, str]]:
    """Return ``(version, sql, checksum)`` sorted by version."""

    result: list[tuple[str, str, str]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        result.append((version, sql, _checksum(sql)))
    return result


def applied_migrations(connection: Any) -> dict[str, str]:
    rows = connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def migrate(connection: Any, *, dry_run: bool = False) -> list[str]:
    """Apply all pending migrations in order.

    Returns the versions applied in this call. When ``dry_run`` is true only
    the pending versions are returned and nothing is written.
    """

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    applied = applied_migrations(connection)
    pending: list[str] = []
    for version, sql, checksum in available_migrations():
        if version in applied:
            if applied[version] != checksum:
                raise ContractError(
                    f"migration {version} content changed after it was applied; refusing to run"
                )
            continue
        if dry_run:
            pending.append(version)
            continue
        with connection:
            connection.executescript(sql)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO schema_migrations"
                "(version, checksum, applied_at) VALUES (?, ?, ?)",
                (version, checksum, utc_now()),
            )
            recorded = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if recorded is None or recorded[0] != checksum:
                raise ContractError(
                    f"migration {version} was concurrently recorded with a different checksum"
                )
        if cursor.rowcount == 1:
            pending.append(version)
    return pending
