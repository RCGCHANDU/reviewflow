"""QA probe (not a test): establish a CheckContext baseline that passes every check.

This exists so the mutation suite starts from a VERIFIED-passing context instead of an
assumed one. Run: `./.venv/Scripts/python.exe tests/qa/_probe_verification_baseline.py`
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.models.contracts import ConsolidatedIssueMap, Issue, IssueResolution, UsageSummary
from reviewflow.models.enums import IssueCategory, Severity
from reviewflow.orchestration.ingest import IngestResult
from reviewflow.verification.checks import CHECKS, CheckContext, run_check
from reviewflow.verification.diff import DiffEvidence, WorkspaceSnapshot

BEFORE = "# Title\n\nA claim about retrieval accuracy.\n\n## Method\n\nWe used a sparse selector.\n"
AFTER = "# Title\n\nA claim about retrieval accuracy (uncertain).\n\n## Method\n\nWe used a sparse selector.\n"


def digest(text: str) -> str:
    """Hash text as UTF-8 bytes.

    Note for reviewers: this is NOT interchangeable with hashing the file on disk. Writing
    text on Windows rewrites LF to CRLF, so an in-memory hash disagrees with the bytes the
    engine sees - which is precisely what the `hash_matches_disk` check is there to catch.
    The baseline therefore hashes the real file for `sha_after`.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(root: Path) -> CheckContext:
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "manuscript.md"
    target.write_text(AFTER, encoding="utf-8")

    context = WorkflowContext(
        workflow_id="wf_probe",
        task_id="task_probe",
        objective="probe",
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
    settings = Settings(workspace_root=workspace, allowed_write_roots=[workspace])
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
    before_snapshot = WorkspaceSnapshot(root=workspace, hashes={"manuscript.md": digest(BEFORE)})
    after_snapshot = WorkspaceSnapshot(root=workspace, hashes={"manuscript.md": digest(AFTER)})

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
        ]
    )
    return CheckContext(
        context=context,
        settings=settings,
        ingest=ingest,
        diff=diff,
        issue_map=issue_map,
        corpus=[issue],
        snapshot_before=before_snapshot,
        snapshot_after=after_snapshot,
        text_before=BEFORE,
        text_after=AFTER,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build(Path(tmp))
        failures = 0
        for name, spec in CHECKS.items():
            result = run_check(spec, ctx)
            marker = "PASS" if result.passed else "FAIL"
            if not result.passed:
                failures += 1
            print(f"  [{marker}] {name:<34} applies={spec.applies(ctx)!s:<5} {result.evidence[:70]!r} {result.error or ''}")
        print(f"\n  baseline failures: {failures}/{len(CHECKS)}")


if __name__ == "__main__":
    main()
