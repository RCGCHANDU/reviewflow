"""Ledger interface and factory.

The workflow depends only on :class:`Ledger`. Swapping SQLite for JSONL or an
in-memory list is a configuration change, not a code change.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from reviewflow.persistence.records import AuditEvent, TraceSpanRecord, WorkflowRunRecord


@runtime_checkable
class Ledger(Protocol):
    """Append-only audit storage for one or more workflow runs."""

    def record(self, event: AuditEvent) -> AuditEvent:
        """Append an event, assigning a per-workflow sequence number."""
        ...

    def record_span(self, span: TraceSpanRecord) -> None:
        """Append a trace span record."""
        ...

    def finalize(self, record: WorkflowRunRecord) -> None:
        """Persist the terminal snapshot of a run."""
        ...

    def events(self, workflow_id: str) -> list[AuditEvent]:
        """All events for one workflow, in append order."""
        ...

    def spans(self, workflow_id: str) -> list[TraceSpanRecord]:
        """All trace spans for one workflow."""
        ...

    def run(self, workflow_id: str) -> WorkflowRunRecord | None:
        """The terminal snapshot, if the run finished."""
        ...

    def close(self) -> None:
        """Release resources. Safe to call more than once."""
        ...


class _SequenceMixin:
    """Shared per-workflow sequence counter."""

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}

    def next_seq(self, workflow_id: str) -> int:
        value = self._seq.get(workflow_id, 0) + 1
        self._seq[workflow_id] = value
        return value


def open_ledger(
    path: Path | str | None = None,
    *,
    backend: str | None = None,
    echo: bool = False,
) -> Ledger:
    """Build a ledger.

    ``backend`` is inferred from the path suffix when omitted:
    ``.sqlite3``/``.db`` -> SQLite, ``.jsonl`` -> JSONL, ``None`` -> in-memory.
    """
    from reviewflow.persistence.jsonl_ledger import JsonlLedger
    from reviewflow.persistence.memory import InMemoryLedger
    from reviewflow.persistence.sqlite_ledger import SqliteLedger

    if path is None and backend is None:
        return InMemoryLedger()

    chosen = backend
    if chosen is None:
        suffix = Path(str(path)).suffix.lower()
        chosen = "sqlite" if suffix in {".sqlite3", ".db", ".sqlite"} else "jsonl"

    if chosen == "memory":
        return InMemoryLedger()
    if chosen == "jsonl":
        return JsonlLedger(Path(str(path)))
    if chosen == "sqlite":
        return SqliteLedger(Path(str(path)), echo=echo)
    raise ValueError(f"unknown ledger backend {chosen!r}")


class LedgerFactory:
    """Callable used by the engine so tests can inject a ledger factory."""

    def __init__(self, path: Path | None = None, backend: str | None = None) -> None:
        self._path = path
        self._backend = backend

    def __call__(self) -> Ledger:
        return open_ledger(self._path, backend=self._backend)

    @classmethod
    def in_memory(cls) -> LedgerFactory:
        return cls(path=None, backend="memory")


def utc_timestamp() -> float:
    return time.time()
