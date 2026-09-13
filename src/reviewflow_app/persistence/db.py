"""Application persistence: SQLite via the standard library.

Deliberate choice, mirroring the engine: ``reviewflow``'s audit ledger is already a
hand-written SQLite store, the schema here is five small tables with no relational
machinery (no joins beyond a run's own rows, no migrations in flight), and a local
single-user tool gains nothing from an ORM but inherits a dependency, a session
lifecycle and a second source of truth about the schema.

What matters for a hosted deployment is not the ORM but the *seam*: every statement lives
behind the repositories in :mod:`reviewflow_app.repositories`, so replacing this module
with Postgres means rewriting one file, not the application.

Concurrency: one short-lived connection per call, WAL enabled. The app is async, and these
statements are sub-millisecond on a local file; the alternative (a thread pool per query)
would add latency and complexity for no measurable benefit at this scale. A hosted
deployment would move this behind a connection pool.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    default_objective TEXT NOT NULL DEFAULT '',
    default_config    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sources (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id);

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_id         TEXT NOT NULL,
    objective         TEXT NOT NULL,
    mode              TEXT NOT NULL,
    status            TEXT NOT NULL,
    current_stage     TEXT,
    engine_workflow_id TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    revision_round    INTEGER NOT NULL DEFAULT 0,
    config            TEXT NOT NULL DEFAULT '{}',
    provider_snapshot TEXT NOT NULL DEFAULT '{}',
    failure_reason    TEXT,
    usage             TEXT NOT NULL DEFAULT '{}',
    result            TEXT NOT NULL DEFAULT '{}',
    workspace_root    TEXT NOT NULL,
    target_path       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS run_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    type      TEXT NOT NULL,
    stage     TEXT,
    payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
"""


class Database:
    """A SQLite database file plus schema management."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection with row access by name and foreign keys enforced."""
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as connection:
            connection.execute(sql, params)


def loads(value: str | None, default: Any) -> Any:
    """Parse a JSON column, tolerating the legacy empty values the schema allows."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
