"""Gate messages must not claim more than the gate can know.

Measured on a real run (6 rounds, blocked): the adversarial reviewer rejected rounds
1-4, PASSED round 5, and rejected round 6. The terminal reason still read
"adversarial reviewer rejected every attempt" - a universal claim contradicted by the
run's own audit log, and the sentence the user's UI displayed as the headline.

The gate is called per round and sees only that round's verdict, so it cannot count
rejections. These tests pin the corrected, evidence-bounded wording and the sibling
branches that must keep naming what they know.
"""

from reviewflow.models.contracts import AdversarialVerdict, VerificationReport, VerificationResult
from reviewflow.models.enums import Stage, WorkflowStatus
from reviewflow.orchestration.workflow import evaluate_completion


def verdict(passed: bool, blocking: int = 0) -> AdversarialVerdict:
    # The gate only measures the length of these lists, so unvalidated placeholders are
    # sufficient fixtures and keep this file independent of the finding schema.
    return AdversarialVerdict.model_construct(
        passed=passed,
        critical_issues=[],
        major_issues=[object() for _ in range(blocking)],
    )


def report(failing: tuple[str, ...] = ()) -> VerificationReport:
    if failing:
        results = [
            VerificationResult(
                check=name,
                command_or_method="test",
                passed=False,
                kind="deterministic",
                mandatory=True,
            )
            for name in failing
        ]
    else:
        results = [
            VerificationResult(
                check="target_exists",
                command_or_method="test",
                passed=True,
                kind="deterministic",
                mandatory=True,
            )
        ]
    return VerificationReport(results=results)


def test_final_round_rejection_does_not_claim_every_attempt() -> None:
    decision = evaluate_completion(
        round_index=6,
        max_rounds=5,
        verification=report(),
        verdict=verdict(passed=False, blocking=1),
        unresolved_blocking=[],
        coverage_degraded=False,
        critical_violation=None,
    )
    assert decision.stage == Stage.BLOCKED
    assert decision.status == WorkflowStatus.BLOCKED
    assert "every attempt" not in decision.reason, decision.reason
    assert "final round" in decision.reason, decision.reason
    assert "1 blocking finding" in decision.reason, decision.reason
    # It must not blame verification either: every mandatory check passed here.
    assert "verification" not in decision.reason, decision.reason


def test_plural_is_correct_for_multiple_findings() -> None:
    decision = evaluate_completion(
        round_index=6,
        max_rounds=5,
        verification=report(),
        verdict=verdict(passed=False, blocking=3),
        unresolved_blocking=[],
        coverage_degraded=False,
        critical_violation=None,
    )
    assert "3 blocking findings" in decision.reason, decision.reason


def test_revise_branch_still_names_the_count() -> None:
    decision = evaluate_completion(
        round_index=2,
        max_rounds=5,
        verification=report(),
        verdict=verdict(passed=False, blocking=2),
        unresolved_blocking=[],
        coverage_degraded=False,
        critical_violation=None,
    )
    assert decision.stage == Stage.REVISE
    assert decision.reason == "adversarial verdict rejected the revision (2 blocking findings)"


def test_passing_verdict_with_unresolved_issues_still_blocks_honestly() -> None:
    class Issue:
        issue_id = "structure_reviewer:st-1"

    decision = evaluate_completion(
        round_index=6,
        max_rounds=5,
        verification=report(),
        verdict=verdict(passed=True),
        unresolved_blocking=[Issue()],
        coverage_degraded=False,
        critical_violation=None,
    )
    assert decision.stage == Stage.BLOCKED
    assert "structure_reviewer:st-1" in decision.reason, decision.reason
    assert "every attempt" not in decision.reason, decision.reason
