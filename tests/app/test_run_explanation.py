"""The terminal explanation must name the gate that actually blocked the run.

Measured on a real run: 14 of 14 mandatory verification checks passed, and the run was
blocked because the adversarial reviewer rejected every attempt - while the explanation
said "verification still refused to pass" and the verification card said "No check
failed". The two contradicted each other, and the first was false.

These tests pin the corrected behaviour and the branch that must NOT change: a blocked
run with a genuinely failing check still says so, by name.
"""

from reviewflow_app.schemas import WorkflowRunSummary
from reviewflow_app.services.runs import RunService


def make_summary(rounds: int = 6) -> WorkflowRunSummary:
    return WorkflowRunSummary(
        id="run_test",
        project_id="prj_test",
        project_name="paper review",
        source_id="src_test",
        source_filename="manuscript.md",
        objective="find contradictions",
        mode="live",
        status="blocked",
        created_at="2026-09-14T21:30:20",
        revision_round=rounds,
    )


def explain(outputs: dict, rounds: int = 6) -> str:
    # explain() is a pure function of its arguments and touches no store, so it is
    # exercised directly rather than through a database fixture.
    return RunService.explain(None, make_summary(rounds), outputs, [object()])


def outputs_with_checks(*, passed: bool, adversarial_rejected: bool = True) -> dict:
    return {
        "terminal": {"rounds_executed": 6, "reason": "adversarial reviewer rejected every attempt"},
        "verification": {
            "results": [
                {"check": "edit_addresses_accepted_issues", "passed": passed},
                {"check": "no_new_unsupported_claims", "passed": True},
            ]
        },
        "adversarial_verdict": {
            "passed": not adversarial_rejected,
            "critical_issues": [],
            "major_issues": ["one major finding"],
        },
        "unresolved_issues": [{"issue_id": "i1"}, {"issue_id": "i2"}],
    }


def test_blocked_with_all_checks_passed_names_the_real_gate() -> None:
    text = explain(outputs_with_checks(passed=True))
    assert "verification still refused to pass" not in text, text
    assert "Every mandatory verification check passed, but the run was blocked" in text, text
    assert "adversarial reviewer rejected every attempt" in text, text


def test_blocked_with_a_failing_check_still_names_the_check() -> None:
    text = explain(outputs_with_checks(passed=False))
    assert "verification still refused to pass: edit_addresses_accepted_issues" in text, text
    assert "adversarial reviewer rejected every attempt" not in text, text


def test_blocked_without_rounds_stays_truthful() -> None:
    text = explain(outputs_with_checks(passed=True), rounds=0)
    assert "Every mandatory verification check passed, but the run was blocked" in text, text
    assert "verification still refused to pass" not in text, text


def test_blocked_never_reads_as_success() -> None:
    text = explain(outputs_with_checks(passed=True))
    assert "blocked" in text, text
    assert "succeeded" not in text, text
