"""SQLite ledger: the default durable backend.

Uses the standard library ``sqlite3`` only. One table for events, one for trace
spans, one for terminal run snapshots. ``detail`` columns hold JSON text.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from reviewflow.persistence.ledger import _SequenceMixin
from reviewflow.persistence.records import AuditEvent, TraceSpanRecord, WorkflowRunRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    stage TEXT,
    agent_role TEXT,
    agent_name TEXT,
    outcome TEXT,
    scope TEXT,
    issue_ids TEXT,
    transition_from TEXT,
    transition_to TEXT,
    verification_status TEXT,
    retry_count INTEGER DEFAULT 0,
    round_index INTEGER DEFAULT 0,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    model TEXT,
    errors TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_workflow ON audit_events(workflow_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON audit_events(workflow_id, event_type);

CREATE TABLE IF NOT EXISTS trace_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_id TEXT,
    span_type TEXT NOT NULL,
    name TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER,
    error TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_workflow ON trace_spans(workflow_id);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    target TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    rounds_executed INTEGER,
    stages_completed TEXT,
    total_tokens INTEGER,
    model_calls INTEGER,
    failed_roles TEXT,
    result_json TEXT,
    created_at REAL
);
"""


class SqliteLedger(_SequenceMixin):
    """Durable ledger backed by a single SQLite file."""

    def __init__(self, path: Path, *, echo: bool = False) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._echo = echo
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # WAL keeps concurrent readers (trace processor + engine) from blocking.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # -- writes ------------------------------------------------------------- #

    def record(self, event: AuditEvent) -> AuditEvent:
        event = event.model_copy(update={"seq": self.next_seq(event.workflow_id)})
        row = (
            event.workflow_id,
            event.seq,
            event.timestamp,
            event.event_type.value,
            event.stage.value if event.stage else None,
            event.agent_role,
            event.agent_name,
            event.outcome.value if event.outcome else None,
            event.scope,
            json.dumps(event.issue_ids),
            event.transition_from.value if event.transition_from else None,
            event.transition_to.value if event.transition_to else None,
            event.verification_status,
            event.retry_count,
            event.round_index,
            event.latency_ms,
            event.input_tokens,
            event.output_tokens,
            event.model,
            json.dumps(event.errors),
            json.dumps(event.detail, default=str),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events (workflow_id, seq, timestamp, event_type, stage, agent_role,"
                " agent_name, outcome, scope, issue_ids, transition_from, transition_to,"
                " verification_status, retry_count, round_index, latency_ms, input_tokens,"
                " output_tokens, model, errors, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._conn.commit()
        if self._echo:
            print(f"[ledger] {event.workflow_id} {event.seq:>4} {event.event_type.value}: {event.detail}")
        return event

    def record_span(self, span: TraceSpanRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trace_spans (workflow_id, trace_id, span_id, parent_id, span_type, name,"
                " started_at, ended_at, duration_ms, error, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    span.workflow_id,
                    span.trace_id,
                    span.span_id,
                    span.parent_id,
                    span.span_type,
                    span.name,
                    span.started_at,
                    span.ended_at,
                    span.duration_ms,
                    span.error,
                    json.dumps(span.detail, default=str),
                ),
            )
            self._conn.commit()

    def finalize(self, record: WorkflowRunRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO workflow_runs (workflow_id, task_id, target, objective, status,"
                " status_reason, rounds_executed, stages_completed, total_tokens, model_calls,"
                " failed_roles, result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.workflow_id,
                    record.task_id,
                    record.target,
                    record.objective,
                    record.status,
                    record.status_reason,
                    record.rounds_executed,
                    json.dumps(record.stages_completed),
                    record.total_tokens,
                    record.model_calls,
                    json.dumps(record.failed_roles),
                    json.dumps(record.result_json, default=str),
                    record.created_at,
                ),
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------- #

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def events(self, workflow_id: str) -> list[AuditEvent]:
        rows = self._query("SELECT * FROM audit_events WHERE workflow_id=? ORDER BY seq", (workflow_id,))
        return [
            AuditEvent(
                workflow_id=row["workflow_id"],
                seq=row["seq"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                stage=row["stage"],
                agent_role=row["agent_role"],
                agent_name=row["agent_name"],
                outcome=row["outcome"],
                scope=row["scope"],
                issue_ids=json.loads(row["issue_ids"] or "[]"),
                transition_from=row["transition_from"],
                transition_to=row["transition_to"],
                verification_status=row["verification_status"],
                retry_count=row["retry_count"] or 0,
                round_index=row["round_index"] or 0,
                latency_ms=row["latency_ms"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                model=row["model"],
                errors=json.loads(row["errors"] or "[]"),
                detail=json.loads(row["detail"] or "{}"),
            )
            for row in rows
        ]

    def spans(self, workflow_id: str) -> list[TraceSpanRecord]:
        rows = self._query("SELECT * FROM trace_spans WHERE workflow_id=? ORDER BY id", (workflow_id,))
        return [
            TraceSpanRecord(
                workflow_id=row["workflow_id"],
                trace_id=row["trace_id"],
                span_id=row["span_id"],
                parent_id=row["parent_id"],
                span_type=row["span_type"],
                name=row["name"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                duration_ms=row["duration_ms"],
                error=row["error"],
                detail=json.loads(row["detail"] or "{}"),
            )
            for row in rows
        ]

    def run(self, workflow_id: str) -> WorkflowRunRecord | None:
        rows = self._query("SELECT * FROM workflow_runs WHERE workflow_id=?", (workflow_id,))
        if not rows:
            return None
        row = rows[0]
        return WorkflowRunRecord(
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            target=row["target"],
            objective=row["objective"],
            status=row["status"],
            status_reason=row["status_reason"] or "",
            rounds_executed=row["rounds_executed"] or 0,
            stages_completed=json.loads(row["stages_completed"] or "[]"),
            total_tokens=row["total_tokens"] or 0,
            model_calls=row["model_calls"] or 0,
            failed_roles=json.loads(row["failed_roles"] or "{}"),
            result_json=json.loads(row["result_json"] or "{}"),
            created_at=row["created_at"] or 0.0,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
