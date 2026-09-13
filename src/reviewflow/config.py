"""Environment-driven configuration.

Everything the workflow can be tuned by lives here, so that no module reads
``os.environ`` directly and tests can construct a fully explicit :class:`Settings`
without touching the environment.

Model names are *not* invented: the defaults below come from the official Agents
SDK "Models" page for ``openai-agents`` 0.22.2, which documents ``gpt-5.4-mini``
as the SDK default model and ``gpt-5.6-sol`` as the recommended higher-quality
model. See ``IMPLEMENTATION_NOTES.md``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Model name that the installed Agents SDK uses when an agent sets no model.
SDK_DEFAULT_MODEL = "gpt-5.4-mini"
#: Recommended higher-quality model per the official Models page.
DEFAULT_STRONG_MODEL = "gpt-5.6-sol"

#: Path globs (relative to workspace root) that must never be edited.
DEFAULT_GENERATED_GLOBS: list[str] = [
    "**/artifacts/**",
    "**/*.generated.*",
    "**/build/**",
    "**/dist/**",
    "**/.venv/**",
]
#: Patterns that must never appear in an edited artifact.
DEFAULT_FORBIDDEN_PATTERNS: list[str] = ["TODO(editor)", "FIXME(editor)", "sk-", "AKIA"]
#: Path globs that constitute the source of truth for ingest.
DEFAULT_SOURCE_GLOBS: list[str] = ["**/*.md", "**/*.txt", "**/*.rst", "**/*.py"]



def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: The six model tiers a role resolves to.
TIER_NAMES: tuple[str, ...] = ("orchestrator", "reviewer", "editor", "adversarial", "cheap", "verifier")


def role_tier(role_key: str, *, write_capable: bool = False, adversarial: bool = False) -> str:
    """Map a role key onto one of the six model tiers.

    Single source of truth for the mapping: both :class:`ModelsByRole` and the
    provider profiles' role defaults resolve through it, so the two can never drift
    apart and a custom registered role automatically inherits a sensible tier.

    Idempotent: a name that *is* a tier resolves to itself. Without this, passing a
    tier where a role key is expected (``role_tier("cheap")``) silently fell through to
    the catch-all and handed the caller the *reviewer* model - which is how the scope
    guardrail ended up running on a model nobody chose for it.
    """
    if role_key in TIER_NAMES:
        return role_key
    if write_capable:
        return "editor"
    if adversarial:
        return "adversarial"
    return "reviewer"


class ModelsByRole(BaseModel):
    """One model per role so a workflow never hard-codes a single model."""

    model_config = ConfigDict(frozen=True)

    orchestrator: str = DEFAULT_STRONG_MODEL
    reviewer: str = SDK_DEFAULT_MODEL
    editor: str = DEFAULT_STRONG_MODEL
    adversarial: str = DEFAULT_STRONG_MODEL
    #: Used for mechanical classification/semantic checks where strong reasoning is waste.
    cheap: str = SDK_DEFAULT_MODEL
    #: Verifier-agent model for semantic verification checks.
    verifier: str = SDK_DEFAULT_MODEL

    def for_role(self, role_key: str, *, write_capable: bool = False, adversarial: bool = False) -> str:
        """Resolve the model for a role key, falling back sensibly for custom roles."""
        tier = role_tier(role_key, write_capable=write_capable, adversarial=adversarial)
        return str(getattr(self, tier))

    def by_tier(self) -> dict[str, str]:
        return {tier: str(getattr(self, tier)) for tier in ("orchestrator", "reviewer", "editor", "adversarial", "cheap", "verifier")}


class RetryLimits(BaseModel):
    """Bounded retries, split by failure class so unrelated errors cannot share a budget."""

    model_config = ConfigDict(frozen=True)

    transient: int = 2
    invalid_output: int = 1
    timeout: int = 1
    #: Logical failures and verification failures are never retried blindly.
    logical: int = 0


class Budget(BaseModel):
    """Explicit cost ceilings. Exceeding a budget stops the workflow, it does not truncate it."""

    model_config = ConfigDict(frozen=True)

    max_revision_rounds: int = 2
    max_turns_per_agent: int = 8
    max_reviewer_concurrency: int = 4
    agent_timeout_s: float = 120.0
    max_total_tokens: int = 400_000
    retries: RetryLimits = Field(default_factory=RetryLimits)


class TracingSettings(BaseModel):
    """Trace configuration.

    ``include_sensitive_data`` defaults to False on purpose: the material under
    review is a document, and trace payloads must not carry it off-host.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    include_sensitive_data: bool = False
    #: Mirror SDK spans into the audit ledger so traces survive without a trace backend.
    to_ledger: bool = True
    workflow_name: str = "reviewflow"


class Settings(BaseModel):
    """Complete workflow configuration."""

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path = Path(".")
    allowed_write_roots: list[Path] = Field(default_factory=lambda: [Path(".")])
    ledger_path: Path = Path("artifacts/audit.sqlite3")

    models: ModelsByRole = Field(default_factory=ModelsByRole)
    budget: Budget = Field(default_factory=Budget)
    tracing: TracingSettings = Field(default_factory=TracingSettings)

    #: Which credentialed backend serves the models: ``auto``, ``codex`` (OpenAI
    #: ChatGPT OAuth), ``commandcode``, or ``openai`` (API key). ``auto`` picks the
    #: first credentialed provider in preference order and needs no configuration,
    #: which is what makes a fresh machine work without editing anything.
    provider: str = "auto"

    #: Role models that were set *explicitly* (by env or by hand). A value here wins
    #: over the provider profile's own defaults, and may carry a ``provider:model``
    #: prefix to pin one role to a different backend than the rest.
    model_overrides: dict[str, str] = Field(default_factory=dict)

    #: Path globs (relative to workspace root) that must never be edited.
    generated_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_GENERATED_GLOBS))
    #: Path globs that constitute the source of truth for ingest.
    source_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_SOURCE_GLOBS))

    #: Patterns that must never appear in an edited artifact.
    forbidden_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PATTERNS))


    #: Minimum number of independent reviewers that must succeed for full coverage.
    min_successful_reviewers: int = 2

    #: Per-agent ``ModelSettings`` overrides, keyed by agent key (``planner``,
    #: ``synthesizer``, ``final_reporter``, ``controlled_editor``, a reviewer role
    #: key, ...). Empty by default: the SDK already applies tuned GPT-5 defaults,
    #: and overriding reasoning/verbosity without measurement would be guesswork.
    model_settings_by_role: dict[str, dict[str, Any]] = Field(default_factory=dict)

    #: Names of the semantic verification checks to request from the verifier agent.
    semantic_checks: list[str] | None = None

    def settings_for_agent(self, agent_key: str, role_key: str) -> dict[str, Any]:
        """Look up model settings by agent key first, then by role key."""
        return dict(self.model_settings_by_role.get(agent_key) or self.model_settings_by_role.get(role_key) or {})

    @field_validator("allowed_write_roots", mode="before")
    @classmethod
    def _coerce_roots(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            return [Path(value)]
        return value

    def resolved(self) -> Settings:
        """Return a copy with every path made absolute and resolved."""
        root = self.workspace_root.expanduser().resolve()
        roots = [r.expanduser().resolve() for r in (self.allowed_write_roots or [root])]
        ledger = self.ledger_path.expanduser()
        ledger = ledger if ledger.is_absolute() else root / ledger
        return self.model_copy(
            update={
                "workspace_root": root,
                "allowed_write_roots": roots,
                "ledger_path": ledger.resolve(),
            }
        )

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``REVIEWFLOW_*`` environment variables."""
        root = Path(_env("REVIEWFLOW_WORKSPACE_ROOT", "."))
        raw_roots = os.environ.get("REVIEWFLOW_ALLOWED_WRITE_ROOTS", "")
        roots = [Path(p) for p in raw_roots.split(os.pathsep) if p.strip()] or [root]
        raw_generated = os.environ.get("REVIEWFLOW_GENERATED_GLOBS", "")
        raw_forbidden = os.environ.get("REVIEWFLOW_FORBIDDEN_PATTERNS", "")
        raw_model_settings = os.environ.get("REVIEWFLOW_MODEL_SETTINGS_JSON", "")
        model_settings = json.loads(raw_model_settings) if raw_model_settings.strip() else {}
        raw_semantic = os.environ.get("REVIEWFLOW_SEMANTIC_CHECKS", "")
        semantic_checks = [name.strip() for name in raw_semantic.split(",") if name.strip()] or None

        settings = cls(
            workspace_root=root,
            allowed_write_roots=roots,
            ledger_path=Path(_env("REVIEWFLOW_LEDGER_PATH", "artifacts/audit.sqlite3")),
            provider=_env("REVIEWFLOW_PROVIDER", "auto").strip().lower(),
            model_overrides={
                # Recorded explicitly so an env-set model beats a provider profile
                # default, while an unset one lets the provider decide.
                tier: os.environ[variable]
                for tier, variable in (
                    ("orchestrator", "REVIEWFLOW_ORCHESTRATOR_MODEL"),
                    ("reviewer", "REVIEWFLOW_REVIEWER_MODEL"),
                    ("editor", "REVIEWFLOW_EDITOR_MODEL"),
                    ("adversarial", "REVIEWFLOW_ADVERSARIAL_MODEL"),
                    ("cheap", "REVIEWFLOW_CHEAP_MODEL"),
                    ("verifier", "REVIEWFLOW_VERIFIER_MODEL"),
                )
                if os.environ.get(variable)
            },
            generated_globs=[p for p in raw_generated.split(",") if p.strip()]
            or list(DEFAULT_GENERATED_GLOBS),
            forbidden_patterns=[p for p in raw_forbidden.split(",") if p.strip()]
            or list(DEFAULT_FORBIDDEN_PATTERNS),
            model_settings_by_role=model_settings,
            semantic_checks=semantic_checks,
            models=ModelsByRole(
                orchestrator=_env("REVIEWFLOW_ORCHESTRATOR_MODEL", DEFAULT_STRONG_MODEL),
                reviewer=_env("REVIEWFLOW_REVIEWER_MODEL", SDK_DEFAULT_MODEL),
                editor=_env("REVIEWFLOW_EDITOR_MODEL", DEFAULT_STRONG_MODEL),
                adversarial=_env("REVIEWFLOW_ADVERSARIAL_MODEL", DEFAULT_STRONG_MODEL),
                cheap=_env("REVIEWFLOW_CHEAP_MODEL", SDK_DEFAULT_MODEL),
                verifier=_env("REVIEWFLOW_VERIFIER_MODEL", SDK_DEFAULT_MODEL),
            ),
            budget=Budget(
                max_revision_rounds=_env_int("REVIEWFLOW_MAX_REVISION_ROUNDS", 2),
                max_turns_per_agent=_env_int("REVIEWFLOW_MAX_TURNS_PER_AGENT", 8),
                max_reviewer_concurrency=_env_int("REVIEWFLOW_MAX_REVIEWER_CONCURRENCY", 4),
                agent_timeout_s=_env_float("REVIEWFLOW_AGENT_TIMEOUT_S", 120.0),
                max_total_tokens=_env_int("REVIEWFLOW_MAX_TOTAL_TOKENS", 400_000),
                retries=RetryLimits(
                    transient=_env_int("REVIEWFLOW_MAX_TRANSIENT_RETRIES", 2),
                    invalid_output=_env_int("REVIEWFLOW_MAX_INVALID_OUTPUT_RETRIES", 1),
                    timeout=_env_int("REVIEWFLOW_MAX_TIMEOUT_RETRIES", 1),
                ),
            ),
            tracing=TracingSettings(
                enabled=_env_bool("REVIEWFLOW_TRACING_ENABLED", True),
                include_sensitive_data=_env_bool("REVIEWFLOW_TRACE_INCLUDE_SENSITIVE_DATA", False),
                to_ledger=_env_bool("REVIEWFLOW_TRACE_TO_LEDGER", True),
            ),
        )
        return settings.resolved()
