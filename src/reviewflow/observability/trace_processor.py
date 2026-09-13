"""Mirror SDK trace spans into the audit ledger.

Keeping a local copy of the span tree means observability does not depend on a
remote trace backend, and — because only identifiers, timings and span types are
stored — the ledger never becomes a second copy of the material under review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.tracing import TracingProcessor, set_trace_processors

from reviewflow.observability.logging_setup import get_logger
from reviewflow.persistence.records import TraceSpanRecord

_log = get_logger("tracing")


def _duration_ms(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:
        return None
    return int((end - start).total_seconds() * 1000)


class LedgerTraceProcessor(TracingProcessor):
    """Writes each finished span to the ledger."""

    def __init__(self, ledger: Any, *, default_workflow_id: str | None = None) -> None:
        self._ledger = ledger
        self._default_workflow_id = default_workflow_id

    def _workflow_id_for(self, span: Any) -> str | None:
        metadata = getattr(span, "trace_metadata", None)
        if isinstance(metadata, dict):
            value = metadata.get("workflow_id")
            if isinstance(value, str) and value:
                return value
        return self._default_workflow_id

    def on_trace_start(self, trace: Any) -> None:
        _log.debug("trace_start", extra={"agent": getattr(trace, "name", None)})

    def on_trace_end(self, trace: Any) -> None:
        _log.debug("trace_end", extra={"agent": getattr(trace, "name", None)})

    def on_span_start(self, span: Any) -> None:
        return None

    def on_span_end(self, span: Any) -> None:
        workflow_id = self._workflow_id_for(span)
        if workflow_id is None:
            return
        span_data = getattr(span, "span_data", None)
        record = TraceSpanRecord(
            workflow_id=workflow_id,
            trace_id=getattr(span, "trace_id", "unknown"),
            span_id=getattr(span, "span_id", "unknown"),
            parent_id=getattr(span, "parent_id", None),
            span_type=getattr(span_data, "type", "unknown"),
            name=getattr(span_data, "name", None),
            started_at=getattr(span, "started_at", None),
            ended_at=getattr(span, "ended_at", None),
            duration_ms=_duration_ms(getattr(span, "started_at", None), getattr(span, "ended_at", None)),
            error=_span_error(span),
            detail=_span_detail(span_data),
        )
        try:
            self._ledger.record_span(record)
        except Exception as exc:  # pragma: no cover - never break a run for telemetry
            _log.warning("ledger span write failed: %s", exc)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


def _span_error(span: Any) -> str | None:
    error = getattr(span, "error", None)
    if error is None:
        return None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _span_detail(span_data: Any) -> dict[str, object]:
    """Extract non-content metadata from a span (tool names, model name, counts)."""
    detail: dict[str, object] = {}
    for field in ("name", "model", "tools", "handoffs", "output_type"):
        value = getattr(span_data, field, None)
        if value is None:
            continue
        if isinstance(value, list):
            detail[field] = [getattr(v, "name", str(v)) for v in value]
        else:
            detail[field] = value if isinstance(value, (str, int, float, bool)) else str(value)
    usage = getattr(span_data, "usage", None)
    if usage is not None:
        detail["usage"] = str(usage)
    return detail


def install_ledger_trace_processor(ledger: Any, *, workflow_id: str | None = None) -> LedgerTraceProcessor:
    """Replace the SDK's trace processors with a ledger-writing one."""
    processor = LedgerTraceProcessor(ledger, default_workflow_id=workflow_id)
    set_trace_processors([processor])
    return processor
