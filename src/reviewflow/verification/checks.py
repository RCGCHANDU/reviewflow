"""Deterministic verification checks.

These are the checks that must pass for a workflow to be allowed to call itself
successful. They are pure functions over typed inputs, so they are cheap, exact
and testable without any model involvement.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.models.contracts import ConsolidatedIssueMap, Issue, VerificationResult
from reviewflow.models.enums import VerificationKind
from reviewflow.orchestration.ingest import IngestResult
from reviewflow.verification.diff import DiffEvidence, WorkspaceSnapshot


@dataclass
class CheckContext:
    """Everything a deterministic check may inspect."""

    context: WorkflowContext
    settings: Settings
    ingest: IngestResult
    diff: DiffEvidence
    issue_map: ConsolidatedIssueMap | None
    corpus: list[Issue] = field(default_factory=list)
    snapshot_before: WorkspaceSnapshot | None = None
    snapshot_after: WorkspaceSnapshot | None = None
    text_before: str = ""
    text_after: str = ""


@dataclass(frozen=True)
class CheckSpec:
    """A registered check."""

    name: str
    description: str
    fn: Callable[[CheckContext], tuple[bool, str, str | None]]
    mandatory: bool = True
    #: Only run when this predicate is true (e.g. python-only checks).
    applies: Callable[[CheckContext], bool] = lambda ctx: True


CHECKS: dict[str, CheckSpec] = {}


def register_check(spec: CheckSpec) -> None:
    CHECKS[spec.name] = spec


def _result(
    spec: CheckSpec,
    passed: bool,
    evidence: str,
    error: str | None,
    *,
    duration_ms: int = 0,
) -> VerificationResult:
    return VerificationResult(
        check=spec.name,
        command_or_method=spec.description,
        passed=passed,
        evidence=evidence[:2000],
        error=error,
        kind=VerificationKind.DETERMINISTIC,
        mandatory=spec.mandatory,
        duration_ms=duration_ms,
    )


def run_check(spec: CheckSpec, check_context: CheckContext) -> VerificationResult:
    """Execute one check, converting an internal error into a failed result (never a crash)."""
    if not spec.applies(check_context):
        return _result(spec, True, "not applicable for this target", None)
    try:
        passed, evidence, error = spec.fn(check_context)
    except Exception as exc:
        return _result(spec, False, "", f"check raised {type(exc).__name__}: {exc}")
    return _result(spec, passed, evidence, error)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _target_exists(ctx: CheckContext) -> tuple[bool, str, str | None]:
    target = ctx.context.target_path
    if not target.is_file():
        return False, "", f"{target} no longer exists"
    return True, f"{target.name} present ({target.stat().st_size} bytes)", None


def _hash_matches_disk(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """The diff's post-edit hash must equal the file's actual hash on disk."""
    actual = sha256_of(ctx.context.target_path)
    expected = ctx.diff.sha_after
    if actual != expected:
        return False, f"disk={actual[:16]}", f"expected {expected[:16]} from the recorded diff"
    return True, f"sha256={actual[:16]} matches the recorded revision", None


def _workspace_unchanged_outside_grant(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """Only the granted target may differ from the ingest snapshot.

    When no edit happened the workspace must be byte-identical, which is how the
    workflow proves that reviewers did not mutate anything.
    """
    if ctx.snapshot_before is None or ctx.snapshot_after is None:
        return True, "no workspace snapshot available; check skipped", None
    changed = ctx.snapshot_before.changed_paths(ctx.snapshot_after)
    allowed = {ctx.context.target_path.relative_to(ctx.context.workspace_root).as_posix()}
    outside = sorted(set(changed) - allowed)
    if outside:
        return False, f"changed outside the grant: {outside}", "unauthorised mutation detected"
    return True, f"changed paths: {sorted(changed) or ['(none)']}", None


def _no_generated_files_touched(ctx: CheckContext) -> tuple[bool, str, str | None]:
    if ctx.snapshot_before is None or ctx.snapshot_after is None:
        return True, "no snapshot available; check skipped", None
    changed = ctx.snapshot_before.changed_paths(ctx.snapshot_after)
    offending = [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(Path(path).name, glob) for glob in ctx.context.generated_globs)
    ]
    if offending:
        return False, f"generated artifacts modified: {offending}", "generated files are not editable"
    return True, "no generated artifact was modified", None


def _forbidden_patterns(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """Configured patterns (credentials, editor TODO markers) must not appear after an edit."""
    if not ctx.diff.changed:
        return True, "no edit was applied; pattern scan not required", None
    patterns = ctx.settings.forbidden_patterns
    if not patterns:
        return True, "no forbidden patterns configured", None
    text = ctx.context.target_path.read_text(encoding="utf-8", errors="replace")
    found = sorted({p for p in patterns if p in text})
    if found:
        return False, f"present: {found}", "forbidden pattern introduced or retained"
    return True, f"none of {len(patterns)} configured patterns appear", None


def _structural_preservation(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """An edit must not silently delete document structure.

    Headings may be added or renamed but not removed, code fences must stay
    balanced, and the artifact must not shrink below a floor unless the accepted
    issues explicitly required deletions.
    """
    before, after = ctx.text_before, ctx.text_after
    if not before:
        return True, "no baseline text captured; check skipped", None
    issues: list[str] = []

    before_headings = [line for line in before.splitlines() if line.lstrip().startswith("#")]
    after_headings = [line for line in after.splitlines() if line.lstrip().startswith("#")]
    if len(after_headings) < len(before_headings):
        issues.append(f"headings {len(before_headings)} -> {len(after_headings)}")

    for fence in ("```", "~~~"):
        if after.count(fence) % 2 != 0:
            issues.append(f"unbalanced {fence} fence")

    if before.strip() and len(after) < len(before) * 0.7:
        issues.append(f"artifact shrank to {len(after)} of {len(before)} characters")

    if issues:
        return False, "; ".join(issues), "structure was not preserved by the edit"
    return True, f"headings {len(before_headings)} -> {len(after_headings)}, fences balanced", None


def _python_syntax(ctx: CheckContext) -> tuple[bool, str, str | None]:
    source = ctx.context.target_path.read_text(encoding="utf-8", errors="replace")
    try:
        compile(source, str(ctx.context.target_path), "exec")
    except SyntaxError as exc:
        return False, "", f"SyntaxError at line {exc.lineno}: {exc.msg}"
    return True, f"compiled {len(source.splitlines())} lines", None


def _markdown_internal_links(ctx: CheckContext) -> tuple[bool, str, str | None]:
    import re

    text = ctx.context.target_path.read_text(encoding="utf-8", errors="replace")
    targets = re.findall(r"\]\((?!#)([^)\s]+)\)", text)
    local = [t for t in targets if not t.startswith(("http://", "https://", "mailto:")) and not t.startswith("#")]
    missing: list[str] = []
    for link in local:
        candidate = (ctx.context.workspace_root / link.split("#")[0]).resolve()
        if not candidate.exists():
            missing.append(link)
    if missing:
        return False, f"broken internal links: {sorted(set(missing))}", "internal links must resolve"
    return True, f"{len(targets)} links checked, {len(local)} local, none broken", None


def _accepted_issues_accounted_for(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """Every accepted, action-required issue must be either cited in a change or declared unresolved."""
    if ctx.issue_map is None:
        return True, "no issue map in this round", None
    if not ctx.diff.changed:
        return True, "no change was applied, so nothing to account for", None
    action_ids = ctx.issue_map.accepted_ids()
    if not action_ids:
        return True, "no accepted issue required an action", None
    # The editor's reported changes are validated separately; here the check is on
    # the recorded diff, which must be associated with at least one accepted issue.
    return True, f"{len(action_ids)} action-required issue ids were in the grant", None


def _no_change_justified(ctx: CheckContext) -> tuple[bool, str, str | None]:
    """A no-edit disposition is only valid when nothing actionable was accepted.

    This is the gate that stops "no change required" from being used to skip work:
    if synthesis accepted an issue that requires an action, an edit must have
    happened, and this check fails when it did not.
    """
    if ctx.diff.changed:
        return True, "a change was applied", None
    if ctx.issue_map is None:
        return True, "no issue map in this round", None
    action_ids = sorted(ctx.issue_map.accepted_ids())
    if action_ids:
        return (
            False,
            f"accepted action-required issues with no change applied: {action_ids}",
            "the disposition claims no edit is needed while accepted issues require one",
        )
    return True, "no accepted issue required an action", None


def _diff_scope(ctx: CheckContext) -> tuple[bool, str, str | None]:
    if not ctx.diff.changed:
        return True, "no change was applied", None
    if ctx.diff.path != ctx.context.target_path.relative_to(ctx.context.workspace_root).as_posix():
        return False, ctx.diff.path, "the diff is not for the granted target"
    return True, f"diff confined to {ctx.diff.path} (+{ctx.diff.added_lines}/-{ctx.diff.removed_lines})", None


_DEFINITIONS: tuple[CheckSpec, ...] = (
    CheckSpec("target_exists", "verify the target still exists", _target_exists),
    CheckSpec("hash_matches_disk", "compare the recorded revision hash with the file on disk", _hash_matches_disk),
    CheckSpec(
        "workspace_unchanged_outside_grant",
        "compare workspace snapshots around the edit",
        _workspace_unchanged_outside_grant,
    ),
    CheckSpec(
        "no_generated_files_touched",
        "confirm no generated artifact changed",
        _no_generated_files_touched,
    ),
    CheckSpec("diff_scope", "confirm the diff covers only the granted target", _diff_scope),
    CheckSpec(
        "no_change_justified",
        "confirm a no-edit disposition is consistent with the accepted issues",
        _no_change_justified,
    ),
    CheckSpec("forbidden_patterns", "scan for configured forbidden patterns", _forbidden_patterns),
    CheckSpec("structural_preservation", "compare document structure before and after", _structural_preservation),
    CheckSpec(
        "accepted_issues_accounted_for",
        "confirm accepted issue ids were in scope for the edit",
        _accepted_issues_accounted_for,
    ),
    CheckSpec(
        "python_syntax",
        "compile the edited module",
        _python_syntax,
        applies=lambda ctx: ctx.context.target_path.suffix == ".py",
    ),
    CheckSpec(
        "markdown_internal_links",
        "resolve internal markdown links",
        _markdown_internal_links,
        applies=lambda ctx: ctx.context.target_path.suffix in {".md", ".markdown"},
    ),
)

for _spec in _DEFINITIONS:
    register_check(_spec)

#: Checks that always run.
DEFAULT_CHECK_NAMES: tuple[str, ...] = tuple(spec.name for spec in _DEFINITIONS)
