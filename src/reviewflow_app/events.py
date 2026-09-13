"""The live event pipe: engine facts -> persisted events -> Server-Sent Events.

Three pieces, deliberately separate:

* :class:`EventBus` - in-process fan-out to whoever is currently watching a run;
* :class:`RunRecorder` - turns engine facts into persisted, browser-safe events and
  publishes them, assigning the sequence numbers a reconnecting client resumes from;
* :class:`PublishingLedger` - a ``Ledger`` decorator, which is how the application
  observes the engine without the engine knowing about the application.

Persistence plus fan-out is the important pairing: a client that connects halfway through
(or reconnects after a refresh) replays the events it missed from the database, and the
live queue only ever needs to carry what has not been persisted yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from reviewflow.observability import get_logger
from reviewflow.persistence.ledger import Ledger
from reviewflow.persistence.records import AuditEvent, EventType, TraceSpanRecord, WorkflowRunRecord
from reviewflow_app.persistence.db import Database
from reviewflow_app.persistence.repositories import EventRepository
from reviewflow_app.schemas import RunEvent

_log = get_logger("app.events")

#: Engine ledger event -> application event type. Anything unlisted is still persisted
#: (the raw event is evidence) but is not given a friendlier name.
LEDGER_EVENT_MAP: dict[str, str] = {
    "stage_start": "stage.started",
    "stage_end": "stage.completed",
    "transition": "stage.transition",
    "agent_call": "agent.completed",
    "tool_call": "tool.called",
    "guardrail": "guardrail.triggered",
    "retry": "agent.retried",
    "failure": "agent.failed",
    "verification": "verification.check",
    "run_finalized": "workflow.terminal",
}

#: Engine event-sink names -> the application's event vocabulary.
#:
#: The engine has two ways to report agent lifecycle: the sink (per attempt, model-level)
#: and the ledger's ``agent_call`` (per completed call, with latency and token usage). They
#: overlap, so the overlap is resolved here rather than in the UI: a fact is reported once,
#: under one name. ``agent_completed`` is deliberately dropped because the ledger's
#: ``agent_call`` event carries strictly more (latency, tokens, retry count) under the same
#: public name.
SINK_EVENT_MAP: dict[str, str | None] = {
    "agent_started": "agent.started",
    "agent_completed": None,
    "agent_failure": "agent.failed",
    "invalid_output": "agent.invalid_output",
    "tool_call": "tool.called",
    "mutation": "edit.mutation",
    "review.report_received": "review.report_received",
    "synthesis.completed": "synthesis.completed",
    "edit.completed": "edit.completed",
    "diff_review.completed": "diff_review.completed",
    "adversarial.completed": "adversarial.completed",
    "verification.completed": "verification.completed",
}

#: Every event type this application emits. Kept explicit so a typo cannot silently create
#: a second name for a fact the UI already renders.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "workflow.created",
        "workflow.started",
        "workflow.succeeded",
        "workflow.blocked",
        "workflow.degraded",
        "workflow.failed",
        "workflow.cancelled",
        "workflow.terminal",
        "stage.started",
        "stage.completed",
        "stage.transition",
        "agent.started",
        "agent.completed",
        "agent.failed",
        "agent.retried",
        "agent.invalid_output",
        "review.report_received",
        "synthesis.completed",
        "edit.completed",
        "edit.mutation",
        "diff_review.completed",
        "adversarial.completed",
        "verification.check",
        "verification.completed",
        "guardrail.triggered",
        "tool.called",
        "stream.end",
    }
)


class EventBus:
    """Fan-out of run events to live subscribers.

    A subscriber is a bounded queue. If a browser stops reading, its queue fills and
    further events are dropped *for that subscriber only* - the events are already
    persisted, so a slow client can always catch up by replaying from its last seen
    sequence number. Blocking the workflow on a slow HTTP reader would be far worse.
    """

    def __init__(self, *, queue_size: int = 512) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent | None]]] = {}
        self._queue_size = queue_size

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue[RunEvent | None]]:
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            yield queue
        finally:
            self._subscribers.get(run_id, set()).discard(queue)

    def publish(self, event: RunEvent) -> None:
        for queue in tuple(self._subscribers.get(event.run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _log.warning(
                    "dropping event for a slow subscriber",
                    extra={"workflow_id": event.run_id, "role": "app"},
                )

    def close(self, run_id: str) -> None:
        """Signal end-of-stream to every subscriber of a finished run."""
        for queue in tuple(self._subscribers.get(run_id, ())):
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - saturated reader
                queue.put_nowait(None)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, ()))


class RunRecorder:
    """Persists and publishes the event stream of exactly one run."""

    def __init__(
        self,
        run_id: str,
        database: Database,
        bus: EventBus,
        *,
        start_seq: int = 0,
        events: EventRepository | None = None,
    ) -> None:
        self.run_id = run_id
        self.database = database
        self.bus = bus
        self.events = events or EventRepository(database)
        self._seq = start_seq

    @classmethod
    def for_run(cls, run_id: str, database: Database, bus: EventBus) -> RunRecorder:
        """Continue a run's sequence where it left off, so seq stays unique after replay."""
        events = EventRepository(database)
        return cls(run_id, database, bus, start_seq=events.max_seq(run_id), events=events)

    def emit(self, event_type: str, *, stage: str | None = None, payload: dict[str, Any] | None = None) -> RunEvent:
        """Persist one event and hand it to live subscribers."""
        self._seq += 1
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() % 1 * 1000):03d}Z"
        body = payload or {}
        event_id = self.events.append(self.run_id, self._seq, timestamp, event_type, stage, body)
        event = RunEvent(
            id=event_id,
            run_id=self.run_id,
            seq=self._seq,
            timestamp=timestamp,
            type=event_type,
            stage=stage,
            payload=body,
        )
        self.bus.publish(event)
        return event

    # -- engine adapters ------------------------------------------------------ #

    def from_audit_event(self, event: AuditEvent) -> RunEvent | None:
        """Translate a ledger event into an application event.

        ``trace_span`` events are skipped: they are tracing detail, and the browser has no
        use for them (they remain in the ledger for developers).
        """
        if event.event_type is EventType.TRACE_SPAN:
            return None
        mapped = LEDGER_EVENT_MAP.get(event.event_type.value, event.event_type.value)
        if event.event_type is EventType.AGENT_CALL and event.outcome is not None and event.outcome.value == "failed":
            mapped = "agent.failed"
        payload: dict[str, Any] = {
            "engine_event": event.event_type.value,
            "outcome": event.outcome.value if event.outcome else None,
            "agent_role": event.agent_role,
            "agent_name": event.agent_name,
            "model": event.model,
            "latency_ms": event.latency_ms,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "retry_count": event.retry_count,
            "round_index": event.round_index,
            "issue_ids": list(event.issue_ids),
            "errors": list(event.errors),
            "verification_status": event.verification_status,
            "transition": (
                {"from": event.transition_from, "to": event.transition_to}
                if event.transition_from or event.transition_to
                else None
            ),
            "detail": event.detail,
        }
        return self.emit(mapped, stage=event.stage.value if event.stage else None, payload=payload)

    def from_sink(self, event: str, detail: dict[str, Any]) -> RunEvent | None:
        """Translate an engine event-sink notification into an application event."""
        if event not in SINK_EVENT_MAP:
            _log.debug("ignoring unknown sink event", extra={"role": "app", "event": event})
            return None
        mapped = SINK_EVENT_MAP[event]
        if mapped is None:
            # Covered by a richer ledger event under the same public name.
            return None
        stage = detail.get("stage")
        if event == "review.report_received":
            stage = "parallel_review"
        return self.emit(mapped, stage=stage if isinstance(stage, str) else None, payload=dict(detail))


class PublishingLedger(Ledger):
    """A ledger decorator that publishes every event it is asked to record.

    This is the whole application/engine integration for live progress: the engine writes
    audit events to whatever ``Ledger`` it was given, and the application supplies one that
    also mirrors them into the run's event stream. No engine code knows the app exists.
    """

    def __init__(
        self,
        inner: Ledger,
        recorder: RunRecorder,
        *,
        on_stage: Callable[[str | None], None] | None = None,
    ) -> None:
        self.inner = inner
        self.recorder = recorder
        #: Called with the new stage whenever the engine enters one, so the run row carries
        #: live progress for anything that cannot watch the event stream (a dashboard, a
        #: history list, a second browser tab).
        self.on_stage = on_stage
        self._publishing = False

    def record(self, event: AuditEvent) -> AuditEvent:
        stored = self.inner.record(event)
        # Guard against re-entrancy: publishing must never recurse back into recording.
        if not self._publishing:
            self._publishing = True
            try:
                self.recorder.from_audit_event(stored)
                if self.on_stage is not None and stored.event_type is EventType.STAGE_START:
                    self.on_stage(stored.stage.value if stored.stage else None)
            except Exception:
                _log.warning("event publication failed", extra={"workflow_id": stored.workflow_id, "role": "app"})
            finally:
                self._publishing = False
        return stored

    def record_span(self, span: TraceSpanRecord) -> None:
        self.inner.record_span(span)

    def finalize(self, record: WorkflowRunRecord) -> None:
        self.inner.finalize(record)

    def events(self, workflow_id: str) -> list[AuditEvent]:
        return self.inner.events(workflow_id)

    def run(self, workflow_id: str) -> WorkflowRunRecord | None:
        return self.inner.run(workflow_id)

    def spans(self, workflow_id: str) -> list[TraceSpanRecord]:
        return self.inner.spans(workflow_id)

    def close(self) -> None:
        self.inner.close()
