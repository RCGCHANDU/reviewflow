"""Repositories: the only place SQL is written.

Route handlers and services speak in models; these classes speak in SQL. That boundary is
what makes the persistence choice reversible (the hosted deployment in the architecture
notes swaps this file, not the application).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from reviewflow_app.persistence.db import Database, dumps, loads
from reviewflow_app.schemas import RunEvent


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(self, *, name: str, description: str, default_objective: str) -> dict[str, Any]:
        now = utcnow()
        project_id = new_id("prj")
        self.db.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at, default_objective, default_config)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, name, description, now, now, default_objective, "{}"),
        )
        created = self.get(project_id)
        assert created is not None
        return created

    def get(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        return dict(row) if row else None

    def list_all(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def touch(self, project_id: str) -> None:
        self.db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project_id))


class SourceRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        *,
        project_id: str,
        filename: str,
        stored_path: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        # The caller may supply the id so the storage path can be derived from it *before*
        # the file is written. Deriving the directory from the filename instead (the original
        # behaviour) meant two uploads of the same name in one project shared a path, and the
        # second silently overwrote the first snapshot.
        source_id = source_id or new_id("src")
        self.db.execute(
            "INSERT INTO sources (id, project_id, filename, stored_path, content_type, size_bytes, sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, project_id, filename, stored_path, content_type, size_bytes, sha256, utcnow()),
        )
        created = self.get(source_id)
        assert created is not None
        return created

    def get(self, source_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return dict(row) if row else None

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM sources WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        return [dict(row) for row in rows]


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        *,
        project_id: str,
        source_id: str,
        objective: str,
        mode: str,
        config: dict[str, Any],
        workspace_root: str,
        target_path: str,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        self.db.execute(
            "INSERT INTO runs (id, project_id, source_id, objective, mode, status, current_stage, created_at,"
            " revision_round, config, workspace_root, target_path) VALUES (?, ?, ?, ?, ?, 'queued', NULL, ?, 0, ?, ?, ?)",
            (run_id, project_id, source_id, objective, mode, utcnow(), dumps(config), workspace_root, target_path),
        )
        created = self.get(run_id)
        assert created is not None
        return created

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._decode(row)

    def list_all(
        self,
        *,
        project_id: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return self._decode_all(rows)

    def mark_started(self, run_id: str, *, engine_workflow_id: str, provider_snapshot: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE runs SET status = 'running', started_at = ?, engine_workflow_id = ?, provider_snapshot = ?,"
            " current_stage = 'ingest', failure_reason = NULL WHERE id = ?",
            (utcnow(), engine_workflow_id, dumps(provider_snapshot), run_id),
        )

    def set_stage(self, run_id: str, stage: str | None) -> None:
        self.db.execute("UPDATE runs SET current_stage = ? WHERE id = ?", (stage, run_id))

    def set_round(self, run_id: str, revision_round: int) -> None:
        self.db.execute("UPDATE runs SET revision_round = ? WHERE id = ?", (revision_round, run_id))

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        failure_reason: str | None = None,
        usage: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, failure_reason = ?, usage = ?, result = ? WHERE id = ?",
            (status, utcnow(), failure_reason, dumps(usage or {}), dumps(result or {}), run_id),
        )

    def annotate(self, run_id: str, **fields: Any) -> None:
        """Update individual columns; used for incremental progress while a run is live."""
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))

    def _decode_all(self, rows: list[Any]) -> list[dict[str, Any]]:
        """Decode a result set, dropping rows that vanished between SELECT and decode.

        A row is only None if it was deleted concurrently, in which case it is not part of
        the result - and the callers are typed as returning models, not optional models.
        """
        decoded = []
        for row in rows:
            item = self._decode(row)
            if item is not None:
                decoded.append(item)
        return decoded

    @staticmethod
    def _decode(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["config"] = loads(data.get("config"), {})
        data["provider_snapshot"] = loads(data.get("provider_snapshot"), {})
        data["usage"] = loads(data.get("usage"), {})
        data["result"] = loads(data.get("result"), {})
        return data


class EventRepository:
    """Append-only event log for one run, and the replay source for reconnects."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def max_seq(self, run_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS seq FROM run_events WHERE run_id = ?", (run_id,))
        return int(row["seq"]) if row else 0

    def append(self, run_id: str, seq: int, timestamp: str, event_type: str, stage: str | None, payload: dict[str, Any]) -> int:
        with self.db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO run_events (run_id, seq, timestamp, type, stage, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, seq, timestamp, event_type, stage, dumps(payload)),
            )
            return int(cursor.lastrowid or 0)

    def list_after(self, run_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[RunEvent]:
        rows = self.db.query(
            "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (run_id, after_seq, limit),
        )
        return [self._decode(row) for row in rows]

    def list_types(self, run_id: str, event_type: str) -> list[RunEvent]:
        rows = self.db.query(
            "SELECT * FROM run_events WHERE run_id = ? AND type = ? ORDER BY seq ASC", (run_id, event_type)
        )
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: Any) -> RunEvent:
        return RunEvent(
            id=int(row["id"]),
            run_id=row["run_id"],
            seq=int(row["seq"]),
            timestamp=row["timestamp"],
            type=row["type"],
            stage=row["stage"],
            payload=loads(row["payload"], {}),
        )


class ArtifactRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        *,
        run_id: str,
        artifact_type: str,
        filename: str,
        stored_path: str,
        sha256: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        artifact_id = new_id("art")
        self.db.execute(
            "INSERT INTO artifacts (id, run_id, type, filename, stored_path, sha256, size_bytes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, run_id, artifact_type, filename, stored_path, sha256, size_bytes, utcnow()),
        )
        created = self.get(artifact_id)
        assert created is not None
        return created

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return dict(row) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC", (run_id,))
        return [dict(row) for row in rows]
