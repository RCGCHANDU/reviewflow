"""JSONL ledger: one JSON object per line, human-readable and greppable."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel

from reviewflow.persistence.ledger import Ledger, _SequenceMixin
from reviewflow.persistence.records import AuditEvent, TraceSpanRecord, WorkflowRunRecord


class JsonlLedger(_SequenceMixin):
    """Writes three companion files: events, spans and runs."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events_path = path.with_suffix(path.suffix + ".events.jsonl")
        self._spans_path = path.with_suffix(path.suffix + ".spans.jsonl")
        self._runs_path = path.with_suffix(path.suffix + ".runs.jsonl")
        self._lock = Lock()
        for file in (self._events_path, self._spans_path, self._runs_path):
            file.touch(exist_ok=True)

    def _append(self, file: Path, payload: dict[str, object]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock, file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def record(self, event: AuditEvent) -> AuditEvent:
        event = event.model_copy(update={"seq": self.next_seq(event.workflow_id)})
        self._append(self._events_path, event.model_dump(mode="json"))
        return event

    def record_span(self, span: TraceSpanRecord) -> None:
        self._append(self._spans_path, span.model_dump(mode="json"))

    def finalize(self, record: WorkflowRunRecord) -> None:
        self._append(self._runs_path, record.model_dump(mode="json"))

    def events(self, workflow_id: str) -> list[AuditEvent]:
        return self._read(self._events_path, AuditEvent, workflow_id)

    def spans(self, workflow_id: str) -> list[TraceSpanRecord]:
        return self._read(self._spans_path, TraceSpanRecord, workflow_id)

    def run(self, workflow_id: str) -> WorkflowRunRecord | None:
        runs = self._read(self._runs_path, WorkflowRunRecord, workflow_id)
        return runs[-1] if runs else None

    def _read(self, file: Path, model: type[BaseModel], workflow_id: str) -> list[Any]:
        out = []
        with self._lock, file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if payload.get("workflow_id") == workflow_id:
                    out.append(model.model_validate(payload))
        return out

    def close(self) -> None:  # nothing to release; files are opened per write
        return None


# The conformance check is a *type* relationship, so it is expressed without constructing
# anything. The previous form - `assert isinstance(JsonlLedger(Path("unused.jsonl")), Ledger)`
# at module scope - ran on import and, because the constructor creates its files, littered
# whatever the current working directory happened to be with empty
# `unused.jsonl.{events,runs,spans}.jsonl` files. Importing a module must not write to disk.
if not issubclass(JsonlLedger, Ledger):  # pragma: no cover - guarding a structural invariant
    raise AssertionError("JsonlLedger must implement Ledger")
