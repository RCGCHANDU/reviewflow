"""QA Phase 4 + Phase 12: verification-safety mutation tests.

The question this file answers: **can the system report SUCCEEDED while a material defect
remains?** (`unsafe_success_rate`, the highest-priority metric in the QA programme.)

Two layers are attacked:

1. the deterministic checks themselves — can each one be made to miss its own violation?
2. the completion gate — can a violated gate still produce SUCCEEDED?

Tests prefixed `test_FINDING_` assert *observed defective behaviour*, not a requirement:
they pin known weaknesses so a future change is visible, and they are defects in the QA
register rather than passing requirements.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from qa_support import baseline, report, verification_result, with_extra_changed_file

from reviewflow.guardrails.final_status import status_inflation
from reviewflow.models.contracts import AdversarialVerdict, UnresolvedIssue, VerificationReport
from reviewflow.models.enums import Severity, Stage, WorkflowStatus
from reviewflow.orchestration.workflow import evaluate_completion
from reviewflow.verification.checks import CHECKS, run_check

# --------------------------------------------------------------------------- #
# 1. The deterministic checks: does each one detect its own violation?
# --------------------------------------------------------------------------- #


def test_baseline_passes_every_registered_check(tmp_path: Path) -> None:
    """Guard for every test below: the baseline must be green, or mutations prove nothing."""
    ctx = baseline(tmp_path)
    failures = {name: run_check(spec, ctx) for name, spec in CHECKS.items() if not run_check(spec, ctx).passed}
    assert failures == {}, f"baseline is not green: {sorted(failures)}"


def test_target_exists_detects_a_deleted_target(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    ctx.context.target_path.unlink()
    assert run_check(CHECKS["target_exists"], ctx).passed is False


def test_hash_matches_disk_detects_a_post_diff_mutation(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    with ctx.context.target_path.open("a", encoding="utf-8") as handle:
        handle.write("\nsilently appended after the diff was recorded\n")
    result = run_check(CHECKS["hash_matches_disk"], ctx)
    assert result.passed is False and "expected" in (result.error or "")


def test_workspace_check_detects_a_change_outside_the_grant(tmp_path: Path) -> None:
    ctx = with_extra_changed_file(baseline(tmp_path), "unrelated.md")
    result = run_check(CHECKS["workspace_unchanged_outside_grant"], ctx)
    assert result.passed is False and "unauthorised" in (result.error or "")


def test_generated_artifact_check_detects_a_touched_artifact(tmp_path: Path) -> None:
    ctx = with_extra_changed_file(baseline(tmp_path), "artifacts/audit_events.json")
    assert run_check(CHECKS["no_generated_files_touched"], ctx).passed is False


def test_diff_scope_detects_a_diff_for_another_file(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    assert run_check(CHECKS["diff_scope"], replace(ctx, diff=replace(ctx.diff, path="other.md"))).passed is False


def test_no_change_justified_detects_work_skipped_by_claiming_nothing_was_needed(tmp_path: Path) -> None:
    """Synthesis accepted an action-required issue; no edit happened. This must fail."""
    ctx = baseline(tmp_path)
    # `DiffEvidence.changed` is sha_before != sha_after - filesystem evidence, not the
    # editor's prose. Emptying the diff text is NOT enough to fake "nothing changed"; the
    # hashes must match too. (Recorded as a positive finding: the property is hash-derived.)
    unchanged = replace(
        ctx.diff, available=True, unified_diff="", added_lines=0, removed_lines=0, sha_after=ctx.diff.sha_before
    )
    result = run_check(CHECKS["no_change_justified"], replace(ctx, diff=unchanged))
    assert result.passed is False
    assert "accepted action-required issues with no change applied" in result.evidence


def test_forbidden_pattern_check_detects_an_introduced_marker(tmp_path: Path) -> None:
    ctx = baseline(tmp_path, forbidden=["SECRET_MARKER"])
    with ctx.context.target_path.open("a", encoding="utf-8") as handle:
        handle.write("\nSECRET_MARKER leaked into the artifact\n")
    assert run_check(CHECKS["forbidden_patterns"], ctx).passed is False


def test_structural_check_detects_a_removed_heading(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    assert run_check(CHECKS["structural_preservation"], replace(ctx, text_after="# Title\n\nbody\n")).passed is False


def test_internal_link_check_detects_a_broken_link(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    with ctx.context.target_path.open("a", encoding="utf-8") as handle:
        handle.write("\nsee [the appendix](appendix.md) for details\n")
    result = run_check(CHECKS["markdown_internal_links"], ctx)
    assert result.passed is False and "appendix.md" in result.evidence


def test_python_syntax_check_detects_invalid_syntax_for_a_python_target(tmp_path: Path) -> None:
    ctx = baseline(tmp_path)
    broken = ctx.context.workspace_root / "module.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    python_ctx = replace(ctx, context=replace(ctx.context, target_path=broken))
    result = run_check(CHECKS["python_syntax"], python_ctx)
    assert result.passed is False and "SyntaxError" in (result.error or "")


def test_FINDING_accepted_issues_accounted_for_cannot_fail(tmp_path: Path) -> None:
    """DEFECT QA-001 (MEDIUM): a mandatory check that no input can make fail.

    `_accepted_issues_accounted_for` computes the accepted action-required issue ids and
    then returns True unconditionally; nothing compares them against what the diff actually
    cited. It is registered `mandatory=True`, so it is counted as a passed check in the
    verification total and in the UI's "N/N mandatory checks passed" figure, while enforcing
    nothing. The real enforcement lives in a *semantic* check
    (`edit_addresses_accepted_issues`) that needs a model call — so the guarantee survives,
    but the deterministic layer's reported strength is overstated.

    This test asserts the defect, not a requirement. It fails if someone fixes the check,
    which is the intended signal to update the defect register.
    """
    ctx = baseline(tmp_path)
    # The issue map requires an edit; the diff shows one; yet no change cites issue ev-1.
    assert run_check(CHECKS["accepted_issues_accounted_for"], ctx).passed is True
    # Even with the issue map demanding action and an empty change, it still passes:
    unchanged = replace(ctx.diff, unified_diff="", added_lines=0, removed_lines=0, sha_after=ctx.diff.sha_before)
    assert run_check(CHECKS["accepted_issues_accounted_for"], replace(ctx, diff=unchanged)).passed is True


def test_FINDING_snapshot_dependent_checks_fail_open_when_snapshots_are_missing(tmp_path: Path) -> None:
    """DEFECT QA-002 (MEDIUM): mandatory checks pass when their evidence is absent.

    `_workspace_unchanged_outside_grant` and `_no_generated_files_touched` return
    ``True, "no snapshot available; check skipped"``. The consequence is that if snapshot
    capture fails or is not supplied, an arbitrary workspace mutation becomes undetectable
    for those rounds — the two checks that exist to catch unauthorised writes report success
    while inspecting nothing. Fail-closed would be False with a "no evidence" error.

    Mitigating fact (verified elsewhere): the workflow does supply snapshots on the normal
    path, so this is a latent bypass rather than an observed one.
    """
    ctx = baseline(tmp_path)
    blind = replace(ctx, snapshot_before=None, snapshot_after=None)
    assert run_check(CHECKS["workspace_unchanged_outside_grant"], blind).passed is True
    assert "skipped" in run_check(CHECKS["workspace_unchanged_outside_grant"], blind).evidence
    assert run_check(CHECKS["no_generated_files_touched"], blind).passed is True


def test_FINDING_structural_check_fails_open_without_baseline_text(tmp_path: Path) -> None:
    """DEFECT QA-003 (LOW): same pattern in another mandatory check.

    With ``text_before`` empty, `_structural_preservation` returns True with "check skipped",
    so an edit that deletes headings or shrinks the artifact to a stub is reported as
    structurally sound.
    """
    ctx = baseline(tmp_path)
    blind = replace(ctx, text_before="", text_after="# Title\n")
    assert run_check(CHECKS["structural_preservation"], blind).passed is True


def test_a_check_that_raises_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """Fail-closed at the runner: an internal error must never read as a passed check."""
    ctx = baseline(tmp_path)
    exploding = replace(CHECKS["target_exists"], fn=lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    result = run_check(exploding, ctx)
    assert result.passed is False
    assert "check raised RuntimeError" in (result.error or "")
    assert result.mandatory is True


def test_an_inapplicable_check_is_reported_explicitly_rather_than_omitted(tmp_path: Path) -> None:
    """A skipped check must be visible as 'not applicable', not silently absent."""
    ctx = baseline(tmp_path)
    result = run_check(CHECKS["python_syntax"], ctx)
    assert result.passed is True
    assert result.evidence == "not applicable for this target"
    assert result.check == "python_syntax"


def test_every_registered_check_applies_to_a_markdown_target_except_the_python_one(tmp_path: Path) -> None:
    """No mandatory check may quietly opt out of a markdown run."""
    ctx = baseline(tmp_path)
    not_applicable = {name for name, spec in CHECKS.items() if not spec.applies(ctx)}
    assert not_applicable == {"python_syntax"}, not_applicable


# --------------------------------------------------------------------------- #
# 2. The completion gate: can a violated gate still produce SUCCEEDED?
# --------------------------------------------------------------------------- #


def gate(**overrides: object):
    """Call evaluate_completion with every gate satisfied, then apply overrides."""
    defaults: dict[str, object] = {
        "round_index": 1,
        "max_rounds": 2,
        "verification": report(verification_result()),
        "verdict": AdversarialVerdict(passed=True, rationale="qa"),
        "unresolved_blocking": [],
        "coverage_degraded": False,
        "critical_violation": None,
    }
    defaults.update(overrides)
    return evaluate_completion(**defaults)  # type: ignore[arg-type]


def test_the_baseline_gate_state_is_success() -> None:
    """Positive control: with every gate satisfied the gate does return SUCCEEDED."""
    decision = gate()
    assert decision.status is WorkflowStatus.SUCCEEDED
    assert decision.stage is Stage.FINAL_REVIEW


def test_no_verification_never_succeeds() -> None:
    assert gate(verification=None).status is WorkflowStatus.BLOCKED
    assert gate(verification=VerificationReport(results=[], stage="verify")).status is WorkflowStatus.BLOCKED


@pytest.mark.parametrize("round_index", [1, 3])
def test_failed_mandatory_check_never_succeeds(round_index: int) -> None:
    failing = report(verification_result("hash_matches_disk", passed=False))
    decision = gate(verification=failing, round_index=round_index, max_rounds=2)
    assert decision.status is not WorkflowStatus.SUCCEEDED
    if round_index < 3:
        # REVISE is a *stage*, deliberately with no terminal status: the run is not over.
        assert decision.stage is Stage.REVISE and decision.status is None
    else:
        assert decision.status is WorkflowStatus.BLOCKED


def test_rejected_adversarial_verdict_never_succeeds() -> None:
    rejected = AdversarialVerdict(passed=False, rationale="fabricated statistic", critical_issues=[])
    assert gate(verdict=rejected, round_index=3).status is WorkflowStatus.BLOCKED
    assert gate(verdict=rejected, round_index=1).stage is Stage.REVISE


def test_unresolved_blocking_issue_never_succeeds() -> None:
    open_issue = UnresolvedIssue(
        issue_id="ev-9", summary="claim still unsupported", severity=Severity.CRITICAL, source="evidence_auditor", reason="qa"
    )
    assert gate(unresolved_blocking=[open_issue], round_index=3).status is WorkflowStatus.BLOCKED
    assert gate(unresolved_blocking=[open_issue], round_index=1).stage is Stage.REVISE


def test_critical_violation_takes_precedence_over_every_passing_gate() -> None:
    decision = gate(critical_violation="unauthorised file changed")
    assert decision.status is WorkflowStatus.BLOCKED
    assert "critical violation" in decision.reason


def test_incomplete_coverage_degrades_and_never_succeeds() -> None:
    decision = gate(coverage_degraded=True)
    assert decision.status is WorkflowStatus.DEGRADED
    assert decision.status is not WorkflowStatus.SUCCEEDED


def test_advisory_check_failure_is_visible_but_does_not_block() -> None:
    """Design decision, recorded explicitly with its risk.

    A failing NON-mandatory check does not block completion. That is intentional (advisory
    checks are heuristic), but it means the safety of the system rests entirely on every
    load-bearing check being registered mandatory. Verified: all 11 registered deterministic
    checks are mandatory=True, so a failure of any of them does block.
    """
    advisory = report(verification_result(), verification_result("semantic_hint", passed=False, mandatory=False))
    assert advisory.mandatory_failures == []
    decision = gate(verification=advisory)
    assert decision.status is WorkflowStatus.SUCCEEDED
    # Visibility: the failure is still in the report for an auditor.
    assert [r.check for r in advisory.results if not r.passed] == ["semantic_hint"]


def test_every_registered_check_is_mandatory() -> None:
    """The invariant the previous test depends on."""
    advisory = [name for name, spec in CHECKS.items() if not spec.mandatory]
    assert advisory == [], f"non-mandatory checks would not gate completion: {advisory}"


# --------------------------------------------------------------------------- #
# 3. The claimed-vs-computed-status guardrail, exhaustively
# --------------------------------------------------------------------------- #


STRENGTH = [
    (WorkflowStatus.FAILED, 0),
    (WorkflowStatus.BLOCKED, 1),
    (WorkflowStatus.DEGRADED, 2),
    (WorkflowStatus.SUCCEEDED, 3),
]


@pytest.mark.parametrize(("claimed", "claimed_rank"), STRENGTH)
@pytest.mark.parametrize(("computed", "computed_rank"), [*STRENGTH, (None, None)])
def test_status_inflation_matrix(claimed: WorkflowStatus, claimed_rank: int, computed: WorkflowStatus | None, computed_rank: int | None) -> None:
    """A claim stronger than the computed status must always be caught."""
    reason = status_inflation(claimed, computed)
    # Fail closed when no gate has run: degraded and above is an unearned claim.
    expected = claimed_rank >= 2 if computed is None else claimed_rank > computed_rank
    assert (reason is not None) is expected, f"claimed={claimed} computed={computed} reason={reason!r}"


def test_a_failed_claim_is_never_flagged_as_inflation() -> None:
    """The guardrails must not fire on honest bad news (that would be a false failure)."""
    assert status_inflation(WorkflowStatus.FAILED, WorkflowStatus.BLOCKED) is None
    assert status_inflation(WorkflowStatus.BLOCKED, WorkflowStatus.SUCCEEDED) is None


# --------------------------------------------------------------------------- #
# 4. Structural: no mandatory check may disappear from a real run's report
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_real_run_reports_every_applicable_check() -> None:
    """End-to-end: the report a real run emits must account for every registered check.

    This is the defence against the "silent skip" bypass: the gate only inspects failures
    that are *present* in the report, so a mandatory check that never ran would leave no
    trace and could not block. The runner must therefore emit an entry per check.
    """
    from reviewflow.evals.scenarios import build_scenarios
    from reviewflow.testkit import run_scripted

    scenario = next(s for s in build_scenarios() if s.name == "successful_end_to_end")
    run = await run_scripted(
        scenario.scripts(),
        material=scenario.material,
        max_revision_rounds=scenario.max_revision_rounds,
    )

    reported = {result.check for result in run.result.verification_results}
    deterministic = set(CHECKS)
    missing = deterministic - reported
    assert missing == set(), f"registered checks absent from the report: {sorted(missing)}"
    assert run.result.status.value == "succeeded", run.result.status.value
