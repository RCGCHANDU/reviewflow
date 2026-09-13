"""API schemas.

Where the engine already owns a contract (reviewer reports, the consolidated issue map,
the adversarial verdict, verification results), this module does **not** redeclare it. Those
objects are already validated Pydantic models with ``extra="forbid"``; mirroring them here
would create a second schema that can silently drift from the one the engine enforces. They
are carried as JSON objects and the field documentation lives with the contract.

What *is* declared here is the application's own language: projects, sources, runs, events,
provider status and preflight results - things the engine does not have.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A run either uses real providers or the engine's deterministic scripted model.
RunMode = Literal["scripted", "live"]

#: Application-level run state. ``cancelled`` and the queued/running pair are the
#: application's own; the rest mirror ``reviewflow.models.enums.WorkflowStatus`` exactly.
RunStatus = Literal["queued", "running", "succeeded", "degraded", "blocked", "failed", "cancelled"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "degraded", "blocked", "failed", "cancelled"})


def _no_blank(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("must not be blank")
    return value.strip()


def from_row(model: type[BaseModel], row: Any, **overrides: Any) -> Any:
    """Build an API model from a database row.

    Database rows legitimately carry columns the API contract does not expose (a stored
    path, for instance). The contracts are ``extra="forbid"`` on purpose, so rows are
    projected onto the model's own fields here rather than weakening the schema or letting
    internal columns leak into a response.
    """
    allowed = model.model_fields
    return model(**{key: value for key, value in dict(row).items() if key in allowed}, **overrides)


# --------------------------------------------------------------------------- #
# projects and sources
# --------------------------------------------------------------------------- #


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    default_objective: str = Field(default="", max_length=4000)

    _validate_name = field_validator("name")(_no_blank)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    default_objective: str


class SourceArtifact(BaseModel):
    """An immutable uploaded source.

    ``sha256`` is the hash of the bytes as uploaded; the run's workspace gets a byte
    identical copy of it, and the engine independently hashes its target before editing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: str
    #: First lines of the file, for a safe preview. Never the whole document.
    preview: str | None = None


# --------------------------------------------------------------------------- #
# run configuration
# --------------------------------------------------------------------------- #


class LimitsConfig(BaseModel):
    """Cost and safety controls exposed to the user, all bounded."""

    model_config = ConfigDict(extra="forbid")

    max_revision_rounds: int = Field(default=2, ge=0, le=10)
    max_turns_per_agent: int = Field(default=8, ge=1, le=40)
    max_reviewer_concurrency: int = Field(default=4, ge=1, le=8)
    agent_timeout_s: float = Field(default=120.0, ge=5.0, le=1800.0)
    max_transient_retries: int = Field(default=2, ge=0, le=5)
    max_invalid_output_retries: int = Field(default=1, ge=0, le=5)
    max_total_tokens: int = Field(default=400_000, ge=1000, le=5_000_000)
    min_successful_reviewers: int = Field(default=2, ge=1, le=8)
    semantic_checks: list[str] | None = None


class RunConfigSnapshot(BaseModel):
    """Exactly what a run was given. Persisted per run so history is reproducible."""

    model_config = ConfigDict(extra="forbid")

    mode: RunMode = "scripted"
    #: Provider for the run: ``auto`` or one of the providers the backend discovered.
    provider: str = "auto"
    #: Role -> model selection. ``auto`` means "whatever the router picks for this role";
    #: anything else is ``provider:model`` (or a bare model id on the active provider).
    role_models: dict[str, str] = Field(default_factory=dict)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    #: Planner-selectable reviewer roles to offer. ``None`` means "all of them".
    reviewers: list[str] | None = None
    #: Scripted runs replay this evaluation scenario (deterministic, no model calls).
    scripted_scenario: str = "successful_end_to_end"

    @field_validator("role_models")
    @classmethod
    def _clean_role_models(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for role, selection in value.items():
            choice = (selection or "").strip()
            # `auto` is represented by absence, so the snapshot stays a diff from defaults.
            if not choice or choice.lower() == "auto":
                continue
            cleaned[role.strip()] = choice
        return cleaned


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    source_id: str
    objective: str = Field(min_length=1, max_length=8000)
    config: RunConfigSnapshot = Field(default_factory=RunConfigSnapshot)
    constraints: list[str] = Field(default_factory=list, max_length=20)

    _validate_objective = field_validator("objective")(_no_blank)


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    source_id: str | None = None
    objective: str = ""
    config: RunConfigSnapshot = Field(default_factory=RunConfigSnapshot)
    #: Whether to run the live provider capability probe (real, billable model calls).
    deep: bool = False


class PreflightCheck(BaseModel):
    """One precondition, with the evidence behind its verdict."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str = ""
    #: True when this failure must stop the run, false when it is advisory.
    blocking: bool = True


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    checks: list[PreflightCheck]
    blocking_failures: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# runs and events
# --------------------------------------------------------------------------- #


class WorkflowRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    project_name: str | None = None
    source_id: str
    source_filename: str | None = None
    objective: str
    mode: RunMode
    status: RunStatus
    current_stage: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    revision_round: int = 0
    failure_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    provider_snapshot: dict[str, Any] = Field(default_factory=dict)


#: The states a stage can be in. Named so the workflow service can build them without
#: repeating the literal, which is what keeps the timeline and the contract in step.
StageStateName = Literal["pending", "running", "completed", "warning", "blocked", "failed", "skipped"]


class StageState(BaseModel):
    """A stage's position in the run, derived from the event stream."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    state: StageStateName = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """A workflow fact, safe to show a browser.

    Deliberately excludes hidden reasoning: the payload carries stage/agent/verification
    facts and already-validated structured outputs, never chain-of-thought.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    seq: int
    timestamp: str
    type: str
    stage: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    type: str
    filename: str
    sha256: str
    size_bytes: int
    created_at: str
    download_url: str


class RunInspection(BaseModel):
    """Everything a UI needs to explain a run, assembled from engine-owned evidence.

    The ``reviewers``, ``synthesis``, ``edit``, ``diff``, ``adversarial`` and
    ``verification`` sections are the engine's own stage outputs, verbatim. They stay
    untyped here so the engine's contracts remain the single definition of their shape.
    """

    model_config = ConfigDict(extra="forbid")

    run: WorkflowRunSummary
    stages: list[StageState]
    plan: dict[str, Any] | None = None
    reviewers: list[dict[str, Any]] = Field(default_factory=list)
    failed_reviewers: dict[str, str] = Field(default_factory=dict)
    synthesis: dict[str, Any] | None = None
    edit: dict[str, Any] | None = None
    diff: str | None = None
    diff_review: dict[str, Any] | None = None
    adversarial: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    final_report: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    unresolved_issues: list[dict[str, Any]] = Field(default_factory=list)
    terminal: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    #: Human-readable explanation of the terminal state, assembled from engine facts.
    status_explanation: str = ""


# --------------------------------------------------------------------------- #
# providers and models
# --------------------------------------------------------------------------- #


class ProviderInfo(BaseModel):
    """Safe provider metadata. Never contains a token, key or credential-store body."""

    model_config = ConfigDict(extra="forbid")

    name: str
    available: bool
    api_mode: str
    requires_streaming: bool
    auth_source: str | None = None
    account: str | None = None
    expires_at: int | None = None
    seconds_left: int | None = None
    refreshable: bool = False
    base_url: str | None = None
    #: Free-text guidance shown when a provider is not usable.
    hint: str = ""


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    label: str
    #: Role tiers this model is a default for on this provider, if any.
    default_for: list[str] = Field(default_factory=list)
    #: Verified capability when a doctor run has been performed, else unknown.
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ProvidersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_provider: str | None
    requested_provider: str
    providers: list[ProviderInfo]
    routing: dict[str, Any] = Field(default_factory=dict)
    role_defaults: dict[str, str] = Field(default_factory=dict)


class DoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    check: str
    ok: bool
    detail: str = ""


class DoctorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_provider: str
    active_provider: str | None
    passed: int
    total: int
    checks: list[DoctorCheck]
    note: str = ""


class RoleInfo(BaseModel):
    """A specialist the engine can run, with its permissions."""

    model_config = ConfigDict(extra="forbid")

    role_key: str
    agent_name: str
    description: str
    focus: list[str] = Field(default_factory=list)
    may_mutate: bool
    allowed_tools: list[str] = Field(default_factory=list)
    mandatory: bool = False
    planner_selectable: bool = True
    default_model: str = ""
    tier: str = ""
