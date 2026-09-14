"""Typed runtime context and mutation authority.

The context object is the SDK's dependency-injection seam: it is passed to
``Runner.run(context=...)`` and reaches every tool and hook, but it is **never**
serialised into a model prompt (the SDK does not send it). That is what makes it
the right home for application state, the audit ledger and the edit grant.

Separation of concerns enforced here:

1. model-visible information -> :mod:`reviewflow.orchestration.packets`
2. application/runtime state -> :class:`WorkflowContext` (this module)
3. secrets                     -> never stored here; read from the environment by the SDK client
4. persistent workflow state   -> :class:`~reviewflow.persistence.ledger.Ledger`
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from reviewflow.config import Settings
from reviewflow.errors import GrantError, PathSecurityError
from reviewflow.models.contracts import RoleUsage, UsageSummary
from reviewflow.models.enums import Stage, WorkflowStatus


@dataclass(frozen=True, slots=True)
class EditGrant:
    """A single-use capability that authorises one controlled edit.

    Issued only by the workflow engine, after synthesis, holding:

    * exactly one target file,
    * the SHA-256 the file must still have,
    * the accepted issue ids the editor may work from,
    * the roots the target must live under,
    * the revision round it was issued for.

    The grant is the application-level authorisation the spec requires: an LLM
    instruction is never sufficient, because the write tool refuses to run
    without a grant that covers the path and the current file hash.
    """

    grant_id: str
    target_path: Path
    expected_sha256: str
    allowed_issue_ids: frozenset[str]
    allowed_roots: tuple[Path, ...]
    round_index: int
    max_changed_lines: int = 400

    def covers(self, path: Path) -> bool:
        """True when ``path`` is exactly the granted target and inside an allowed root."""
        resolved = path.expanduser().resolve()
        if resolved != self.target_path:
            return False
        return any(_is_within(resolved, root) for root in self.allowed_roots)

    def check_hash(self, actual_sha256: str) -> None:
        """Raise when the file changed since the grant was issued (optimistic locking)."""
        if actual_sha256 != self.expected_sha256:
            raise GrantError(
                f"target {self.target_path.name} changed since the grant was issued "
                f"(expected {self.expected_sha256[:12]}, found {actual_sha256[:12]}); refusing to write"
            )

    def check_issues(self, issue_ids: list[str]) -> None:
        """Raise when the edit's issue citations are not authorised by this grant.

        An edit that cites *no* issue while the grant authorises some is rejected too. The
        grant exists to tie every change to an accepted finding; an empty list satisfied the
        previous check vacuously, so a caller could obtain an authorised write by citing
        nothing at all.
        """
        if not issue_ids and self.allowed_issue_ids:
            allowed = ", ".join(sorted(self.allowed_issue_ids))
            raise GrantError(
                f"edit cites no issue id; every change must name the accepted issue(s) it "
                f"addresses (grant authorises: {allowed})"
            )
        unknown = sorted(set(issue_ids) - set(self.allowed_issue_ids))
        if unknown:
            raise GrantError(f"edit cites issue ids outside the grant: {unknown}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:  # pragma: no cover - py<3.9 defensive path
        return False


def sha256_of(path: Path) -> str:
    """Hash a file's bytes. Used for ingest snapshots and grant enforcement."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class WorkflowContext:
    """Mutable application state for one workflow run.

    Deliberately a plain dataclass, not a Pydantic model: it holds live objects
    (the ledger, open file handles by path) and is never serialised whole.
    """

    workflow_id: str
    task_id: str
    objective: str
    target_path: Path
    workspace_root: Path
    allowed_write_roots: list[Path]
    generated_globs: list[str]
    constraints: list[str]
    min_successful_reviewers: int
    source_sha256: str
    stage: Stage = Stage.INGEST
    round_index: int = 0
    edit_grant: EditGrant | None = None
    usage: UsageSummary = field(default_factory=UsageSummary)
    #: Free-text preferences that shape the final report's tone, never its facts.
    user_preferences: dict[str, str] = field(default_factory=dict)
    #: Registry of verification checks requested for this run.
    verification_config: dict[str, object] = field(default_factory=dict)
    #: Roles that failed and why, for coverage reporting.
    failed_roles: dict[str, str] = field(default_factory=dict)
    #: The status the engine has computed from its gates. Authoritative.
    computed_status: WorkflowStatus | None = None
    #: Honest limitations to disclose in the final report (truncation, degraded material).
    disclosures: list[str] = field(default_factory=list)
    #: Monotonic counter used to keep generated ids unique inside a run.
    call_counter: int = 0
    #: Structured output of each stage, captured as the run progresses so a finished
    #: run can be inspected (and re-inspected later) without re-running it. Values are
    #: JSON-safe dumps of the stage's own Pydantic contract - the engine still owns the
    #: objects; this is evidence, not state the workflow reads back.
    stage_outputs: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    #: Full settings for this run (models, budgets, tracing). Not model-visible.
    settings: Settings | None = None
    #: Model provider for nested runs started outside the invoker (guardrail agent,
    #: agent-as-tool). This is how test/eval scripting stays in force everywhere.
    model_provider: Any = None
    #: Optional sink used by tools to append audit events without importing the ledger.
    audit_hook: Callable[[str, dict[str, Any]], None] | None = None

    def audit(self, event: str, **detail: Any) -> None:
        """Record a tool-level audit event, if a sink was supplied."""
        if self.audit_hook is not None:
            self.audit_hook(event, detail)

    @classmethod
    def create(
        cls,
        *,
        objective: str,
        target_path: Path,
        workspace_root: Path,
        allowed_write_roots: list[Path],
        constraints: list[str] | None = None,
        generated_globs: list[str] | None = None,
        min_successful_reviewers: int = 2,
        user_preferences: dict[str, str] | None = None,
        verification_config: dict[str, object] | None = None,
    ) -> WorkflowContext:
        resolved_target = target_path.expanduser().resolve()
        root = workspace_root.expanduser().resolve()
        roots = [r.expanduser().resolve() for r in allowed_write_roots] or [root]
        if not _is_within(resolved_target, root):
            raise PathSecurityError(f"target {resolved_target} is outside workspace root {root}")
        return cls(
            workflow_id=f"wf_{uuid.uuid4().hex[:12]}",
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            objective=objective.strip(),
            target_path=resolved_target,
            workspace_root=root,
            allowed_write_roots=roots,
            generated_globs=list(generated_globs or []),
            constraints=list(constraints or []),
            min_successful_reviewers=min_successful_reviewers,
            source_sha256=sha256_of(resolved_target),
            user_preferences=dict(user_preferences or {}),
            verification_config=dict(verification_config or {}),
        )

    # -- capability helpers ------------------------------------------------- #

    def next_call_id(self, prefix: str) -> str:
        self.call_counter += 1
        return f"{prefix}-{self.call_counter:04d}"

    def issue_grant(self, target: Path, expected_sha256: str, issue_ids: list[str], *, round_index: int) -> EditGrant:
        """Mint an edit grant. Only the engine calls this."""
        resolved = target.expanduser().resolve()
        if not any(_is_within(resolved, root) for root in self.allowed_write_roots):
            raise PathSecurityError(f"{resolved} is outside the allowed write roots {self.allowed_write_roots}")
        self.edit_grant = EditGrant(
            grant_id=f"grant_{uuid.uuid4().hex[:10]}",
            target_path=resolved,
            expected_sha256=expected_sha256,
            allowed_issue_ids=frozenset(issue_ids),
            allowed_roots=tuple(self.allowed_write_roots),
            round_index=round_index,
        )
        return self.edit_grant

    def advance_grant_hash(self, sha256_after: str) -> None:
        """Move the active grant's expected hash forward after its own write.

        The grant guards against a change made *outside* this grant. Once the
        grant's own tool has written the file, the previous hash is stale by
        definition, so refusing the next edit would block legitimate sequential
        edits by the single authorised writer. The grant id, target and issue ids
        are unchanged, so the authority is not widened by this.
        """
        grant = self.require_grant()
        self.edit_grant = replace(grant, expected_sha256=sha256_after)

    def revoke_grant(self) -> None:
        """Grants are single-round: revoke at every stage exit that follows an edit."""
        self.edit_grant = None

    def require_grant(self) -> EditGrant:
        if self.edit_grant is None:
            raise GrantError("no active edit grant; the workflow has not authorised a mutation")
        return self.edit_grant

    def record_usage(self, role: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
        self.usage.add_call(role, input_tokens, output_tokens, latency_ms)

    def role_usage(self, role: str) -> RoleUsage:
        return self.usage.by_role.get(role, RoleUsage(role=role))

    def note_failed_role(self, role_key: str, reason: str) -> None:
        self.failed_roles[role_key] = reason

    @property
    def coverage_complete(self) -> bool:
        return not self.failed_roles

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)
