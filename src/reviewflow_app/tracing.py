"""Trace routing for concurrent runs.

The Agents SDK keeps **one global list of trace processors**, while this application can
have several runs in flight at once, each with its own audit ledger. Installing a
per-run processor would mean every run after the first silently sends its spans into
whichever ledger was installed last.

So the app installs a single routing processor for the process: it reads the run id the
engine already stamps into every span's ``trace_metadata`` (``RunConfig`` sets it) and
writes the span to that run's ledger. That keeps tracing local - nothing is exported to a
remote backend - and correct under concurrency.
"""

from __future__ import annotations

import threading
from typing import Any

from agents.tracing import TracingProcessor, set_trace_processors

from reviewflow.observability import get_logger
from reviewflow.observability.trace_processor import LedgerTraceProcessor

_log = get_logger("app.tracing")


class RunRoutingTraceProcessor(TracingProcessor):
    """Dispatches spans to the ledger of the run that produced them."""

    def __init__(self) -> None:
        self._ledgers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(self, workflow_id: str, ledger: Any) -> None:
        with self._lock:
            self._ledgers[workflow_id] = ledger

    def unregister(self, workflow_id: str) -> None:
        with self._lock:
            self._ledgers.pop(workflow_id, None)

    def _route(self, span: Any) -> LedgerTraceProcessor | None:
        metadata = getattr(span, "trace_metadata", None)
        workflow_id = metadata.get("workflow_id") if isinstance(metadata, dict) else None
        if not isinstance(workflow_id, str) or not workflow_id:
            return None
        with self._lock:
            ledger = self._ledgers.get(workflow_id)
        if ledger is None:
            return None
        return LedgerTraceProcessor(ledger, default_workflow_id=workflow_id)

    # -- TracingProcessor ----------------------------------------------------- #

    def on_trace_start(self, trace: Any) -> None:
        return None

    def on_trace_end(self, trace: Any) -> None:
        return None

    def on_span_start(self, span: Any) -> None:
        return None

    def on_span_end(self, span: Any) -> None:
        processor = self._route(span)
        if processor is None:
            # No ledger registered (a nested run from a shutdown, say). Dropping the span
            # is correct: routing it to another run's ledger would be worse than losing it.
            return
        try:
            processor.on_span_end(span)
        except Exception:
            _log.warning("span routing failed", extra={"role": "app"})

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


_ROUTER: RunRoutingTraceProcessor | None = None


def install_trace_router() -> RunRoutingTraceProcessor:
    """Install the process-wide router once and return it."""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = RunRoutingTraceProcessor()
        set_trace_processors([_ROUTER])
    return _ROUTER


def current_router() -> RunRoutingTraceProcessor | None:
    return _ROUTER
