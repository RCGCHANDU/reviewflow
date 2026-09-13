"""Evaluation scenarios.

Each scenario is a scripted end-to-end run of the *real* workflow with measurable
pass criteria. The point is regression testing behavioural guarantees (blocking,
disclosure, independence, bounded loops) rather than checking that objects
instantiate.

How to read a scenario:

* ``material`` is the artifact under review, written into a throwaway workspace.
* ``scripts`` maps an *agent key* to the ordered payloads that agent will return.
  A single agent run can consume several payloads (one per model call), so an
  editor that must make two edits scripts ``apply_edit``, ``apply_edit``, then the
  ``edit_result``: each payload answers the previous tool result.
* ``assertion`` inspects the returned result *and* the audit ledger. Assertions on
  the ledger are what make behaviour - not just outcome - testable.

Adding a scenario: append a :class:`Scenario` describing material, scripts, the
expected terminal status and the assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from reviewflow.fixtures import (
    apply_edit,
    change,
    conflict,
    diff_review,
    edit_result,
    final_report,
    finding,
    issue,
    issue_map,
    plan,
    report,
    resolution,
    verdict,
    verification,
)
from reviewflow.models.enums import Stage, WorkflowStatus
from reviewflow.simulate import hang, scope_allow, text
from reviewflow.testkit import HEDGED_CLAIM, UNSUPPORTED_CLAIM, ScriptedRun, Workspace

#: Text the "editor inserts a fabricated claim" scenario writes.
FABRICATED_CLAIM = "our method reaches 99.1% answer accuracy on the full test split"

CHECK_NAMES = ("edit_addresses_accepted_issues", "no_new_unsupported_claims", "uncertainty_preserved")

#: Quote for the reproducibility issue in the end-to-end scenario. It is copied from one
#: physical line of the material, because ``apply_edit`` matches bytes exactly.
BASELINE_SENTENCE = "vision-language model, so the comparison isolates the selector"
BASELINE_SENTENCE_FIXED = (
    "vision-language model (the version is not recorded in this draft), "
    "so the comparison isolates the selector"
)


@dataclass
class Scenario:
    """One evaluation case."""

    name: str
    description: str
    criteria: list[str]
    expected_status: WorkflowStatus
    scripts: Callable[[], dict[str, list]]
    assertion: Callable[[ScriptedRun], list[str]]
    material: str = ""
    max_revision_rounds: int = 2
    verification_config: dict[str, object] | None = None
    workspace: Workspace | None = None
    tags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Material
# --------------------------------------------------------------------------- #

MATERIAL = f"""\
# Sparse Retrieval for Long-Video Question Answering

## Abstract

We introduce a sparse retrieval front-end for long-video question answering that
selects 12 keyframes per query. On the Haystack-LVBench 3600-second split, {UNSUPPORTED_CLAIM}.

## Method

We sample candidate frames at 1 fps and score each frame with a CLIP-style
encoder. The top-12 frames are passed to a vision-language model.

Our approach is evaluated against a uniform-sampling baseline that uses the same
vision-language model, so the comparison isolates the selector.

## Limitations

Frame selection is bounded by the encoder's vocabulary, and we do not claim that
our results transfer to other benchmarks.

## Conclusion

Sparse selection is sufficient for long-video understanding.
"""

CLEAN_MATERIAL = """\
# Summary of the Retrieval Ablation

## Method

We compare two selectors while holding the vision-language model fixed, so the
comparison isolates the selector. The sample is described in the method section
and its size is stated.

## Limitations

The sample covers one benchmark, and we make no claim about transfer.
"""


# --------------------------------------------------------------------------- #
# Ledger helpers shared by the assertions
# --------------------------------------------------------------------------- #


def _stage_ends(run: ScriptedRun, stage: Stage) -> list[Any]:
    return [
        event for event in run.events if event.event_type.value == "stage_end" and event.stage is stage
    ]


def _agent_events(run: ScriptedRun, role: str) -> list[Any]:
    return [event for event in run.events if event.event_type.value == "agent_call" and event.agent_role == role]


def _transitions(run: ScriptedRun) -> list[tuple[str, str]]:
    return [
        (event.transition_from.value, event.transition_to.value)
        for event in run.events
        if event.event_type.value == "transition" and event.transition_from and event.transition_to
    ]


def _mutations(run: ScriptedRun) -> list[Any]:
    return [
        event
        for event in run.events
        if event.event_type.value == "tool_call" and event.detail.get("event") == "mutation"
    ]


def _verification_events(run: ScriptedRun, status: str) -> list[Any]:
    return [
        event
        for event in run.events
        if event.event_type.value == "verification" and event.verification_status == status
    ]


# --------------------------------------------------------------------------- #
# 1. clean input: no edit required, but every gate still runs
# --------------------------------------------------------------------------- #


def _assert_clean(run: ScriptedRun) -> list[str]:
    failures = []
    if run.model.calls_for("controlled_editor"):
        failures.append("the editor must not run when no change is required")
    if _mutations(run):
        failures.append("a mutation was recorded although no edit was required")
    if not _stage_ends(run, Stage.VERIFY):
        failures.append("the no-edit path skipped verification")
    if (Stage.SYNTHESIZE.value, Stage.ADVERSARIAL_REVIEW.value) not in _transitions(run):
        failures.append("the no-edit decision was not adversarially reviewed")
    if not any(result.passed for result in run.result.verification_results):
        failures.append("no verification check ran")
    if run.result.final_output is None or run.result.final_output.claimed_status is not WorkflowStatus.SUCCEEDED:
        failures.append("the final report did not claim the computed status")
    if run.result.unresolved_issues:
        failures.append(f"a clean artifact produced unresolved issues: {[i.issue_id for i in run.result.unresolved_issues]}")
    return failures


SCENARIO_CLEAN = Scenario(
    name="clean_input_no_edit",
    description="material with nothing actionable: no edit, but verification and adversarial review still run",
    criteria=[
        "status is succeeded",
        "the controlled editor is never invoked",
        "no mutation appears in the audit ledger",
        "the no-change decision is adversarially reviewed",
        "mandatory verification passes",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=CLEAN_MATERIAL,
    tags=["happy-path", "no-edit"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [
            report("evidence_auditor", assessment="every quantitative statement is either cited or hedged")
        ],
        "structure_reviewer": [report("structure_reviewer", assessment="progression and limitations are coherent")],
        "synthesizer": [
            issue_map(accepted=[], rejected=[], change_required=False, change_rationale="no issue requires an edit")
        ],
        "adversarial_reviewer": [
            verdict(
                passed=True,
                rationale="attacked the no-change decision and could not find a defect that requires an edit",
            )
        ],
        "final_reporter": [
            final_report(summary="nothing actionable; no edit was applied", claimed_status="succeeded")
        ],
    },
    assertion=_assert_clean,
)


# --------------------------------------------------------------------------- #
# 2. an unresolved reviewer disagreement blocks success
# --------------------------------------------------------------------------- #


def _assert_conflict(run: ScriptedRun) -> list[str]:
    failures = []
    if (Stage.VERIFY.value, Stage.BLOCKED.value) not in _transitions(run):
        failures.append("verify did not move to blocked")
    disagreements = run.result.final_output.reviewer_disagreements if run.result.final_output else []
    if not disagreements:
        failures.append("the disagreement was not disclosed in the final report")
    if not any("disagree" in item.summary for item in run.result.unresolved_issues):
        failures.append("the disagreement is missing from unresolved_issues")
    if run.result.status is WorkflowStatus.SUCCEEDED:
        failures.append("a conflict must never be smoothed into success")
    return failures


SCENARIO_CONFLICT = Scenario(
    name="conflicting_reviewers",
    description="one reviewer flags a critical issue, another judges the same text acceptable, and synthesis cannot settle it",
    criteria=[
        "the conflict is recorded rather than resolved away",
        "the fixed issue is still verified, but status is blocked",
        "the final report discloses the disagreement",
    ],
    expected_status=WorkflowStatus.BLOCKED,
    material=MATERIAL,
    # Zero revision rounds: one settled edit plus an open disagreement isolates the cause.
    max_revision_rounds=0,
    tags=["synthesis", "disclosure"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "technical_reviewer"])],
        "evidence_auditor": [report("evidence_auditor", issues=[issue("ev-1")])],
        "technical_reviewer": [
            report(
                "technical_reviewer",
                issues=[],
                assessment="the figure is provisional and the surrounding text makes that clear; no defect",
            )
        ],
        "synthesizer": [
            issue_map(
                accepted=[resolution("evidence_auditor:ev-1")],
                conflicts=[conflict(["evidence_auditor:ev-1"], adjudicated=False)],
                change_required=True,
                change_rationale="the issue is actionable, but the disagreement about it remains open",
            )
        ],
        "controlled_editor": [
            apply_edit(
                path="manuscript.md",
                old_text=UNSUPPORTED_CLAIM,
                new_text=HEDGED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before=UNSUPPORTED_CLAIM,
                        after=HEDGED_CLAIM,
                    )
                ],
                unresolved=["the reviewers still disagree about whether the original wording was adequate"],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True)],
        "adversarial_reviewer": [
            verdict(passed=True, rationale="the edit is small and justified, but the disagreement is real")
        ],
        "verifier": [verification()],
    },
    assertion=_assert_conflict,
)


# --------------------------------------------------------------------------- #
# 3. an unsupported claim is detected, accepted, fixed and verified
# --------------------------------------------------------------------------- #


def _assert_fix(run: ScriptedRun) -> list[str]:
    failures = []
    if HEDGED_CLAIM not in run.workspace.text():
        failures.append("the accepted issue was not actually fixed in the file")
    mutations = _mutations(run)
    if not mutations:
        failures.append("no mutation was recorded")
    elif mutations[0].detail.get("issue_ids") != ["evidence_auditor:ev-1"]:
        failures.append("the mutation did not cite the accepted issue id")
    if (Stage.SYNTHESIZE.value, Stage.CONTROLLED_EDIT.value) not in _transitions(run):
        failures.append("synthesis did not route to the controlled edit")
    if run.result.unresolved_issues:
        failures.append(
            f"unresolved issues remain after a verified fix: {[i.issue_id for i in run.result.unresolved_issues]}"
        )
    return failures


SCENARIO_UNSUPPORTED_CLAIM = Scenario(
    name="unsupported_claim_detected_and_fixed",
    description="an uncited quantitative claim is detected, accepted, fixed and verified",
    criteria=[
        "the issue is accepted with observed evidence",
        "the edit cites the accepted issue id",
        "the fix is present in the file",
        "status is succeeded with no unresolved issues",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=MATERIAL,
    tags=["happy-path", "edit"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [report("evidence_auditor", issues=[issue("ev-1")])],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [issue_map(accepted=[resolution("evidence_auditor:ev-1")])],
        "controlled_editor": [
            apply_edit(
                path="manuscript.md",
                old_text=UNSUPPORTED_CLAIM,
                new_text=HEDGED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change("c1", issue_ids=["evidence_auditor:ev-1"], before=UNSUPPORTED_CLAIM, after=HEDGED_CLAIM)
                ],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True, notes=["the change is confined to the claim"])],
        "adversarial_reviewer": [verdict(passed=True, rationale="the edit only weakens an uncited claim")],
        "verifier": [verification()],
        "final_reporter": [
            final_report(
                summary="one critical unsupported claim was fixed and verified",
                accepted=["evidence_auditor:ev-1 (critical): marked the figure as uncited"],
                changes=["abstract: the 97.3% figure is now marked preliminary and uncited"],
                verification_summary=["all mandatory checks passed"],
                claimed_status="succeeded",
            )
        ],
    },
    assertion=_assert_fix,
)


# --------------------------------------------------------------------------- #
# 4. a reviewer's invented evidence is rejected during synthesis
# --------------------------------------------------------------------------- #


def _assert_hallucination(run: ScriptedRun) -> list[str]:
    failures = []
    if run.model.calls_for("controlled_editor"):
        failures.append("an edit was made although every issue was rejected")
    if _mutations(run):
        failures.append("a mutation was recorded although every issue was rejected")
    rejected = run.result.final_output.rejected_issues_summary if run.result.final_output else []
    if not rejected:
        failures.append("the rejection was not disclosed in the final report")
    if run.result.unresolved_issues:
        failures.append("a rejected issue must not appear as unresolved")
    return failures


SCENARIO_HALLUCINATION_REJECTED = Scenario(
    name="reviewer_hallucination_rejected",
    description="a reviewer files a critical issue whose quoted evidence does not exist; synthesis rejects it",
    criteria=[
        "the fabricated issue is rejected, not acted on",
        "no edit is made",
        "the rejection is disclosed",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=MATERIAL,
    tags=["synthesis", "evidence"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [
            report(
                "evidence_auditor",
                issues=[
                    issue(
                        "ev-1",
                        description="the material reports results on a hidden test split",
                        evidence="quoted: 'we report 99.1% on the held-out test split (Appendix D)'",
                        recommendation="remove the claim",
                    )
                ],
                assessment="one claim about a test split is unsupported",
            )
        ],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [
            issue_map(
                accepted=[],
                rejected=[
                    resolution(
                        "evidence_auditor:ev-1",
                        disposition="rejected",
                        rationale="the quoted sentence appears nowhere in the material and there is no Appendix D",
                        action_required=False,
                        assigned_to="orchestrator",
                    )
                ],
                change_required=False,
                change_rationale="the only issue rests on a quotation that does not exist",
            )
        ],
        "adversarial_reviewer": [verdict(passed=True, rationale="no change was made and none was needed")],
        "final_reporter": [
            final_report(
                summary="no actionable issue was found; one reviewer claim was rejected as unsupported",
                rejected=["evidence_auditor:ev-1 rejected: the quoted sentence does not exist in the material"],
                claimed_status="succeeded",
            )
        ],
    },
    assertion=_assert_hallucination,
)


# --------------------------------------------------------------------------- #
# 5. the editor fabricates, the adversarial reviewer catches it, round 2 fixes it
# --------------------------------------------------------------------------- #


def _assert_adversarial_catch(run: ScriptedRun) -> list[str]:
    failures = []
    if run.result.rounds_executed < 2:
        failures.append("the workflow did not re-enter the revision loop")
    if (Stage.VERIFY.value, Stage.REVISE.value) not in _transitions(run):
        failures.append("verify did not request a revision")
    verdicts = [
        event.detail.get("passed")
        for event in _stage_ends(run, Stage.ADVERSARIAL_REVIEW)
        if "passed" in event.detail
    ]
    if False not in verdicts:
        failures.append("the adversarial reviewer never rejected the fabricated revision")
    if True not in verdicts:
        failures.append("the adversarial reviewer never accepted the corrected revision")
    text_after = run.workspace.text()
    if FABRICATED_CLAIM in text_after:
        failures.append("the fabricated assertion survived into the final artifact")
    if not _verification_events(run, "no_new_unsupported_claims:fail"):
        failures.append("the semantic verification never flagged the fabricated claim in any round")
    return failures


SCENARIO_ADVERSARIAL_CATCH = Scenario(
    name="editor_fabricates_adversarial_catches",
    description="the editor introduces an unsupported assertion; adversarial review and verification reject round 1, round 2 corrects it",
    criteria=[
        "the fabricated assertion is caught before completion",
        "at least one revision round runs",
        "the adversarial verdict flips from rejected to accepted",
        "the fabricated text is absent from the final artifact",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=MATERIAL,
    tags=["adversarial", "loop"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [report("evidence_auditor", issues=[issue("ev-1")])],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [
            issue_map(accepted=[resolution("evidence_auditor:ev-1")]),
            issue_map(accepted=[resolution("evidence_auditor:ev-1")]),
        ],
        "controlled_editor": [
            # Round 1: the "fix" replaces an uncited figure with a tighter, invented one.
            apply_edit(
                path="manuscript.md",
                old_text=UNSUPPORTED_CLAIM,
                new_text=FABRICATED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
                rationale="make the claim precise",
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before=UNSUPPORTED_CLAIM,
                        after=FABRICATED_CLAIM,
                        rationale="sharpen the claim",
                    )
                ],
            ),
            # Round 2: the correction removes the invented number.
            apply_edit(
                path="manuscript.md",
                old_text=FABRICATED_CLAIM,
                new_text=HEDGED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
                rationale="remove the invented evaluation and mark the figure uncited",
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before=FABRICATED_CLAIM,
                        after=HEDGED_CLAIM,
                        rationale="the previous edit invented an evaluation that never happened",
                    )
                ],
            ),
        ],
        "diff_reviewer": [
            diff_review(approved=True, notes=["the change is small and localised"]),
            diff_review(approved=True, notes=["the correction removes the invented number"]),
        ],
        "adversarial_reviewer": [
            verdict(
                passed=False,
                critical=[
                    finding(
                        "the revision states a full-test-split accuracy that the material never reported and that the method section contradicts",
                        location="abstract",
                        evidence=f"'{FABRICATED_CLAIM}'",
                        issue_id="evidence_auditor:ev-1",
                    )
                ],
                rationale="the revision claims a completed evaluation that did not happen",
                rejection_reasons=["fabricated evaluation result"],
            ),
            verdict(passed=True, rationale="the invented number is gone and the remaining claim is marked provisional"),
        ],
        "verifier": [
            verification(overrides={"no_new_unsupported_claims": False}),
            verification(),
        ],
        "final_reporter": [
            final_report(
                summary="round 1 introduced a fabricated result which adversarial review rejected; round 2 corrected it",
                accepted=["evidence_auditor:ev-1 (critical): the figure is now marked uncited"],
                changes=["the invented test-split number was removed and the claim hedged"],
                verification_summary=["round 1 failed no_new_unsupported_claims; round 2 passed"],
                claimed_status="succeeded",
            )
        ],
    },
    assertion=_assert_adversarial_catch,
)


# --------------------------------------------------------------------------- #
# 6. a failed mandatory verification can never become success
# --------------------------------------------------------------------------- #


def _assert_verification_blocks(run: ScriptedRun) -> list[str]:
    failures = []
    if run.result.status is not WorkflowStatus.BLOCKED:
        failures.append(f"a failing mandatory check must block, got {run.result.status.value}")
    failed = {result.check for result in run.result.verification_results if not result.passed}
    if "structural_preservation" not in failed:
        failures.append(f"structural_preservation should have failed, failed checks: {sorted(failed)}")
    if not _verification_events(run, "structural_preservation:fail"):
        failures.append("the failing check was not recorded in the audit ledger")
    if run.result.status is WorkflowStatus.SUCCEEDED:
        failures.append("success was reported despite a failed mandatory check")
    return failures


SCENARIO_VERIFICATION_BLOCKS = Scenario(
    name="failed_verification_blocks_success",
    description="the editor deletes document structure, a mandatory check fails, and the workflow ends blocked",
    criteria=[
        "structural_preservation fails",
        "the failure is persisted in the ledger",
        "status is blocked and never succeeded",
    ],
    expected_status=WorkflowStatus.BLOCKED,
    material=MATERIAL,
    max_revision_rounds=1,
    tags=["verification", "blocking"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [report("evidence_auditor", issues=[issue("ev-1")])],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [
            issue_map(accepted=[resolution("evidence_auditor:ev-1")]),
            issue_map(accepted=[resolution("evidence_auditor:ev-1")]),
        ],
        "controlled_editor": [
            apply_edit(
                path="manuscript.md",
                old_text="## Limitations\n\n",
                new_text="",
                issue_ids=["evidence_auditor:ev-1"],
                rationale="compress the document",
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before="## Limitations\n\n",
                        after="",
                        kind="delete",
                        rationale="compress the document",
                    )
                ],
            ),
            apply_edit(
                path="manuscript.md",
                old_text="## Conclusion\n\n",
                new_text="",
                issue_ids=["evidence_auditor:ev-1"],
                rationale="compress the document further",
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c2",
                        issue_ids=["evidence_auditor:ev-1"],
                        before="## Conclusion\n\n",
                        after="",
                        kind="delete",
                        rationale="compress the document further",
                    )
                ],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True), diff_review(approved=True)],
        "adversarial_reviewer": [
            verdict(passed=True, rationale="the change resolves the issue"),
            verdict(passed=True, rationale="the change resolves the issue"),
        ],
        "verifier": [verification(), verification()],
    },
    assertion=_assert_verification_blocks,
)


# --------------------------------------------------------------------------- #
# 7. a reviewer timeout degrades the run
# --------------------------------------------------------------------------- #


def _assert_timeout(run: ScriptedRun) -> list[str]:
    failures = []
    if run.result.status is WorkflowStatus.SUCCEEDED:
        failures.append("missing reviewer coverage was reported as success")
    if "evidence_auditor" not in run.result.review_coverage.failed:
        failures.append(f"coverage does not list the failed reviewer: {run.result.review_coverage.model_dump()}")
    if not run.result.failures:
        failures.append("the failure was not recorded on the result")
    if not [event for event in _agent_events(run, "evidence_auditor") if event.outcome and event.outcome.value == "failed"]:
        failures.append("the reviewer failure was not recorded in the ledger")
    return failures


SCENARIO_REVIEWER_TIMEOUT = Scenario(
    name="reviewer_timeout_degrades",
    description="one reviewer hangs past its timeout; the workflow finishes its gates but reports degraded coverage",
    criteria=[
        "the timeout is recorded as a failure in the ledger",
        "coverage lists the reviewer as failed",
        "status is degraded, never succeeded",
    ],
    expected_status=WorkflowStatus.DEGRADED,
    material=MATERIAL,
    tags=["failure", "coverage"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [hang(5.0)],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [
            issue_map(accepted=[], rejected=[], change_required=False, change_rationale="nothing actionable")
        ],
        "adversarial_reviewer": [verdict(passed=True, rationale="no change was needed")],
        "final_reporter": [
            final_report(
                summary="coverage is degraded because one reviewer timed out",
                disclosures=["the evidence auditor timed out; its perspective is missing"],
                claimed_status="degraded",
            )
        ],
    },
    assertion=_assert_timeout,
)


# --------------------------------------------------------------------------- #
# 8. malformed structured output is recovered, not coerced
# --------------------------------------------------------------------------- #


def _assert_malformed(run: ScriptedRun) -> list[str]:
    failures = []
    events = _agent_events(run, "evidence_auditor")
    if not [event for event in events if event.retry_count >= 1]:
        failures.append("the malformed response was not retried with corrective feedback")
    if len(run.model.calls_for("evidence_auditor")) < 2:
        failures.append("the reviewer was not called twice")
    if run.result.status is not WorkflowStatus.SUCCEEDED:
        failures.append(f"a recovered reviewer should allow success, got {run.result.status.value}")
    return failures


SCENARIO_MALFORMED_OUTPUT = Scenario(
    name="malformed_output_recovered",
    description="a reviewer returns prose instead of JSON; the SDK rejects it and the bounded retry recovers",
    criteria=[
        "the retry is recorded with retry_count >= 1",
        "the workflow still completes with full coverage",
        "status is succeeded",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=MATERIAL,
    tags=["failure", "schema"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [plan(specialists=["evidence_auditor", "structure_reviewer"])],
        "evidence_auditor": [
            text("I could not produce JSON, here is prose instead"),
            report("evidence_auditor", issues=[issue("ev-1")]),
        ],
        "structure_reviewer": [report("structure_reviewer")],
        "synthesizer": [issue_map(accepted=[resolution("evidence_auditor:ev-1")])],
        "controlled_editor": [
            apply_edit(
                path="manuscript.md",
                old_text=UNSUPPORTED_CLAIM,
                new_text=HEDGED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change("c1", issue_ids=["evidence_auditor:ev-1"], before=UNSUPPORTED_CLAIM, after=HEDGED_CLAIM)
                ],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True)],
        "adversarial_reviewer": [verdict(passed=True, rationale="no blocking defect")],
        "verifier": [verification()],
        "final_reporter": [
            final_report(summary="the malformed response was corrected and the fix verified", claimed_status="succeeded")
        ],
    },
    assertion=_assert_malformed,
)


# --------------------------------------------------------------------------- #
# 9. full end-to-end run with two accepted issues
# --------------------------------------------------------------------------- #


def _assert_end_to_end(run: ScriptedRun) -> list[str]:
    failures = []
    expected_stages = [
        Stage.INGEST,
        Stage.PLAN,
        Stage.PARALLEL_REVIEW,
        Stage.SYNTHESIZE,
        Stage.CONTROLLED_EDIT,
        Stage.DIFF_REVIEW,
        Stage.ADVERSARIAL_REVIEW,
        Stage.VERIFY,
        Stage.FINAL_REVIEW,
        Stage.COMPLETE,
    ]
    missing = [stage for stage in expected_stages if stage not in run.result.stages_completed]
    if missing:
        failures.append(f"stages never entered: {[s.value for s in missing]}")
    roles = {event.agent_role for event in run.events if event.agent_role}
    for role in ("evidence_auditor", "technical_reviewer", "rhetorical_analyst", "structure_reviewer", "verifier"):
        if role not in roles:
            failures.append(f"role {role} was planned but never called")
    mutation_ids = {
        issue_id
        for event in run.events
        if event.detail.get("event") == "mutation"
        for issue_id in (event.detail.get("issue_ids") or [])
    }
    if mutation_ids != {"evidence_auditor:ev-1", "technical_reviewer:tech-1"}:
        failures.append(f"both accepted issues should have been edited, saw {sorted(mutation_ids)}")
    if BASELINE_SENTENCE_FIXED not in run.workspace.text():
        failures.append("the second accepted issue was not fixed in the file")
    if not run.result.verification_results:
        failures.append("verification produced no results")
    if run.result.final_output is None:
        failures.append("no final report was produced")
    if not run.result.artifacts:
        failures.append("no artifacts were persisted")
    record = run.ledger.run(run.result.workflow_id)
    if record is None:
        failures.append("the terminal run record was not persisted in the ledger")
    elif record.status != run.result.status.value:
        failures.append("the persisted status disagrees with the returned status")
    return failures


SCENARIO_END_TO_END = Scenario(
    name="successful_end_to_end",
    description="full run: parallel review by four specialists, synthesis of two accepted issues, two edits, diff review, adversarial review, verification, final report",
    criteria=[
        "every stage from ingest to complete is entered",
        "all four specialists are exercised",
        "both accepted issues are edited, one run, two tool calls",
        "the audit ledger holds the terminal record and the returned status matches it",
    ],
    expected_status=WorkflowStatus.SUCCEEDED,
    material=MATERIAL,
    tags=["happy-path", "full"],
    scripts=lambda: {
        "scope_guardrail": [scope_allow()],
        "planner": [
            plan(specialists=["evidence_auditor", "technical_reviewer", "rhetorical_analyst", "structure_reviewer"])
        ],
        "evidence_auditor": [report("evidence_auditor", issues=[issue("ev-1")])],
        "technical_reviewer": [
            report(
                "technical_reviewer",
                issues=[
                    issue(
                        "tech-1",
                        severity="major",
                        category="reproducibility",
                        description="the comparison claims to isolate the selector, but the model version used for both arms is not stated, so it cannot be reproduced",
                        evidence="quoted: 'uses the same vision-language model, so the comparison isolates the selector'",
                        recommendation="state which version was used for both arms, or record that it is undocumented",
                        location="L21-L40",
                        unit_id="u2",
                    )
                ],
            )
        ],
        "rhetorical_analyst": [
            report("rhetorical_analyst", assessment="information flow is clear and terminology is stable")
        ],
        "structure_reviewer": [
            report("structure_reviewer", assessment="abstract, method, limitations and conclusion line up")
        ],
        "synthesizer": [
            issue_map(
                accepted=[
                    resolution("evidence_auditor:ev-1"),
                    resolution(
                        "technical_reviewer:tech-1",
                        rationale="the comparison cannot be reproduced without the model version, so the gap must be declared",
                        assigned_to="controlled_editor",
                    ),
                ],
                change_required=True,
            )
        ],
        "controlled_editor": [
            # One run, two edits: model call, tool result, model call, tool result, final result.
            apply_edit(
                path="manuscript.md",
                old_text=UNSUPPORTED_CLAIM,
                new_text=HEDGED_CLAIM,
                issue_ids=["evidence_auditor:ev-1"],
                rationale="mark the uncited figure as provisional",
            ),
            apply_edit(
                path="manuscript.md",
                old_text=BASELINE_SENTENCE,
                new_text=BASELINE_SENTENCE_FIXED,
                issue_ids=["technical_reviewer:tech-1"],
                rationale="declare the undocumented version instead of implying a controlled comparison",
            ),
            edit_result(
                "manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before=UNSUPPORTED_CLAIM,
                        after=HEDGED_CLAIM,
                        rationale="the figure has no source, so it must not read as established",
                    ),
                    change(
                        "c2",
                        issue_ids=["technical_reviewer:tech-1"],
                        before=BASELINE_SENTENCE,
                        after=BASELINE_SENTENCE_FIXED,
                        rationale="declare the undocumented version rather than invent it",
                    ),
                ],
                unresolved=[],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True, notes=["both hunks map to an accepted issue"])],
        "adversarial_reviewer": [
            verdict(
                passed=True,
                minor=[finding("the model version is still unknown, but it is now declared", location="method")],
                rationale="no fabrication, no scope inflation, no removed caveat",
            )
        ],
        "verifier": [verification()],
        "final_reporter": [
            final_report(
                summary="two accepted issues were fixed and verified: an uncited figure is now marked provisional, and an undocumented model version is declared",
                accepted=[
                    "evidence_auditor:ev-1 (critical): fixed",
                    "technical_reviewer:tech-1 (major): fixed",
                ],
                changes=[
                    "abstract: the 97.3% figure is marked preliminary and uncited",
                    "method: the comparison now states that the model version is undocumented",
                ],
                verification_summary=["all mandatory checks passed"],
                claimed_status="succeeded",
            )
        ],
    },
    assertion=_assert_end_to_end,
)


def build_scenarios() -> list[Scenario]:
    """Every evaluation case, in run order."""
    return [
        SCENARIO_CLEAN,
        SCENARIO_CONFLICT,
        SCENARIO_UNSUPPORTED_CLAIM,
        SCENARIO_HALLUCINATION_REJECTED,
        SCENARIO_ADVERSARIAL_CATCH,
        SCENARIO_VERIFICATION_BLOCKS,
        SCENARIO_REVIEWER_TIMEOUT,
        SCENARIO_MALFORMED_OUTPUT,
        SCENARIO_END_TO_END,
    ]


def scenario_by_name(name: str) -> Scenario:
    for scenario in build_scenarios():
        if scenario.name == name:
            return scenario
    raise KeyError(f"unknown scenario {name!r}; available: {[s.name for s in build_scenarios()]}")
