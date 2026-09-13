"""In-memory ledger, used by tests and by ``--ledger none`` runs."""

from __future__ import annotations

from reviewflow.persistence.ledger import Ledger, _SequenceMixin
from reviewflow.persistence.records import AuditEvent, TraceSpanRecord, WorkflowRunRecord


class InMemoryLedger(_SequenceMixin):
    """Keeps everything in lists. Not durable; ideal for unit tests and evals."""

    def __init__(self) -> None:
        super().__init__()
        self._events: dict[str, list[AuditEvent]] = {}
        self._spans: dict[str, list[TraceSpanRecord]] = {}
        self._runs: dict[str, WorkflowRunRecord] = {}
        self.closed = False

    def record(self, event: AuditEvent) -> AuditEvent:
        event = event.model_copy(update={"seq": self.next_seq(event.workflow_id)})
        self._events.setdefault(event.workflow_id, []).append(event)
        return event

    def record_span(self, span: TraceSpanRecord) -> None:
        self._spans.setdefault(span.workflow_id, []).append(span)

    def finalize(self, record: WorkflowRunRecord) -> None:
        self._runs[record.workflow_id] = record

    def events(self, workflow_id: str) -> list[AuditEvent]:
        return list(self._events.get(workflow_id, []))

    def spans(self, workflow_id: str) -> list[TraceSpanRecord]:
        return list(self._spans.get(workflow_id, []))

    def run(self, workflow_id: str) -> WorkflowRunRecord | None:
        return self._runs.get(workflow_id)

    def close(self) -> None:
        self.closed = True

    # Test helpers ---------------------------------------------------------- #

    def event_types(self, workflow_id: str) -> list[str]:
        return [e.event_type.value for e in self.events(workflow_id)]

    def stages(self, workflow_id: str) -> list[str]:
        return [str(e.stage) for e in self.events(workflow_id) if e.event_type.value == "stage_start"]


assert isinstance(InMemoryLedger(), Ledger)
