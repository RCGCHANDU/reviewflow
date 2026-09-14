"""Shared fixtures for the QA suites.

Establishes a CheckContext baseline that passes every registered verification check, so a
mutation test can change exactly one fact and attribute the resulting failure to that fact.
Verified by `_probe_verification_baseline.py` before this module was relied on.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.models.contracts import (
    ConsolidatedIssueMap,
    Issue,
    IssueResolution,
    UsageSummary,
    VerificationReport,
    VerificationResult,
)
from reviewflow.models.enums import IssueCategory, Severity, VerificationKind
from reviewflow.orchestration.ingest import IngestResult
from reviewflow.verification.checks import CheckContext
from reviewflow.verification.diff import DiffEvidence, WorkspaceSnapshot

BEFORE = "# Title\n\nA claim about retrieval accuracy.\n\n## Method\n\nWe used a sparse selector.\n"
AFTER = "# Title\n\nA claim about retrieval accuracy (uncertain).\n\n## Method\n\nWe used a sparse selector.\n"


def digest(text: str) -> str:
    """Hash text as UTF-8 bytes (not the same as the file on disk: CRLF translation)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def baseline(root: Path, *, forbidden: list[str] | None = None) -> CheckContext:
    """A context in which every registered check passes. Verified, not assumed."""
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "manuscript.md"
    target.write_text(AFTER, encoding="utf-8")

    context = WorkflowContext(
        workflow_id="wf_qa",
        task_id="task_qa",
        objective="QA baseline",
        target_path=target,
        workspace_root=workspace,
        allowed_write_roots=[workspace],
        generated_globs=["artifacts/**"],
        constraints=[],
        min_successful_reviewers=4,
        source_sha256=digest(BEFORE),
        usage=UsageSummary(),
        user_preferences=[],
        verification_config=None,
        failed_roles={},
        disclosures=[],
        started_at=0.0,
    )
    settings = Settings(
        workspace_root=workspace,
        allowed_write_roots=[workspace],
        **({"forbidden_patterns": forbidden} if forbidden is not None else {}),
    )
    ingest = IngestResult(
        relative_path="manuscript.md",
        text=BEFORE,
        sha256=digest(BEFORE),
        line_count=BEFORE.count("\n"),
        char_count=len(BEFORE),
        unit_hints=[],
        warnings=[],
    )
    diff = DiffEvidence(
        path="manuscript.md",
        available=True,
        unified_diff="--- a/manuscript.md\n+++ b/manuscript.md\n@@\n-a claim\n+a claim (uncertain)\n",
        added_lines=1,
        removed_lines=1,
        sha_before=digest(BEFORE),
        sha_after=sha256_of(target),
        bytes_before=len(BEFORE.encode()),
        bytes_after=len(AFTER.encode()),
    )
    issue = Issue(
        id="ev-1",
        location="manuscript.md",
        category=IssueCategory.MISSING_UNCERTAINTY,
        severity=Severity.MAJOR,
        description="The accuracy claim is stated without uncertainty.",
        evidence="the claim reads as established",
        evidence_status="observed",
        recommendation="mark it as uncertain",
        confidence="high",
        source_reviewer="evidence_auditor",
    )
    issue_map = ConsolidatedIssueMap(
        change_required=True,
        change_rationale="one accepted issue requires an edit",
        accepted_issues=[
            IssueResolution(
                issue_id="ev-1",
                disposition="accepted",
                rationale="the claim is overstated",
                action_required=True,
                assigned_to="controlled_editor",
            )
        ],
    )
    return CheckContext(
        context=context,
        settings=settings,
        ingest=ingest,
        diff=diff,
        issue_map=issue_map,
        corpus=[issue],
        snapshot_before=WorkspaceSnapshot(root=workspace, hashes={"manuscript.md": digest(BEFORE)}),
        snapshot_after=WorkspaceSnapshot(root=workspace, hashes={"manuscript.md": sha256_of(target)}),
        text_before=BEFORE,
        text_after=AFTER,
    )


def with_extra_changed_file(ctx: CheckContext, name: str) -> CheckContext:
    """Record a second file as changed between the snapshots."""
    workspace = ctx.context.workspace_root
    (workspace / name).parent.mkdir(parents=True, exist_ok=True)
    (workspace / name).write_text("mutated by something that is not the editor\n", encoding="utf-8")
    hashes = dict(ctx.snapshot_after.hashes) if ctx.snapshot_after else {}
    hashes[name] = digest("mutated by something that is not the editor\n")
    return replace(ctx, snapshot_after=WorkspaceSnapshot(root=workspace, hashes=hashes))


def verification_result(
    name: str = "target_exists",
    *,
    passed: bool = True,
    mandatory: bool = True,
) -> VerificationResult:
    return VerificationResult(
        check=name,
        command_or_method="qa",
        passed=passed,
        evidence="qa",
        error=None if passed else "qa failure",
        kind=VerificationKind.DETERMINISTIC,
        mandatory=mandatory,
    )


def report(*results: VerificationResult) -> VerificationReport:
    return VerificationReport(results=list(results), stage="verify")
