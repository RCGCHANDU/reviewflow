"""Ledger record shapes."""

from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from reviewflow.models.enums import Stage, StageOutcome


class EventType(str, Enum):
    """Kinds of ledger events. Kept small so querying stays trivial."""

    STAGE_START = "stage_start"
    STAGE_END = "stage_end"
    TRANSITION = "transition"
    AGENT_CALL = "agent_call"
    TOOL_CALL = "tool_call"
    GUARDRAIL = "guardrail"
    RETRY = "retry"
    FAILURE = "failure"
    VERIFICATION = "verification"
    TRACE_SPAN = "trace_span"
    RUN_FINALIZED = "run_finalized"


class AuditEvent(BaseModel):
    """One immutable row in the audit trail."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    seq: int = 0
    timestamp: float = Field(default_factory=time.time)
    event_type: EventType
    stage: Stage | None = None
    agent_role: str | None = None
    agent_name: str | None = None
    outcome: StageOutcome | None = None
    scope: str | None = None
    issue_ids: list[str] = Field(default_factory=list)
    transition_from: Stage | None = None
    transition_to: Stage | None = None
    verification_status: str | None = None
    retry_count: int = 0
    round_index: int = 0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    errors: list[str] = Field(default_factory=list)
    detail: dict[str, object] = Field(default_factory=dict)


class TraceSpanRecord(BaseModel):
    """An SDK trace span mirrored into the ledger.

    Span payloads are not stored: only identifiers, timings and span type, so the
    ledger never becomes a copy of the document under review.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    span_type: str
    name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class WorkflowRunRecord(BaseModel):
    """Terminal snapshot of a workflow run."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    task_id: str
    target: str
    objective: str
    status: str
    status_reason: str
    rounds_executed: int
    stages_completed: list[str]
    total_tokens: int
    model_calls: int
    failed_roles: dict[str, str] = Field(default_factory=dict)
    result_json: dict[str, object] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
