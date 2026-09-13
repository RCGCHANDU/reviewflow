"""Application-level validators: the semantics SDK guardrails cannot check.

These are the checks that stand between an agent's JSON and a state transition, so
they are tested for both directions: what they must reject, and what they must
repair *with a disclosure* rather than silently coerce.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewflow.fixtures import (
    change,
    conflict,
    edit_result,
    issue,
    issue_map,
    plan,
    report,
    resolution,
    unit,
    verdict,
)
from reviewflow.models.contracts import (
    AdversarialFinding,
    AdversarialVerdict,
    Issue,
    ReviewerReport,
    ReviewPlan,
    VerificationReport,
    VerificationResult,
)
from reviewflow.models.enums import Severity
from reviewflow.validation import (
    check_reviewer_report,
    mandatory_change_required,
    normalize_reviewer_report,
    validate_adversarial_verdict,
    validate_edit_result,
    validate_issue_map,
    validate_plan,
    validate_verification_report,
)

UNITS = {"u1": "critical", "u2": "major"}
REGISTRY_KEYS = {"evidence_auditor", "technical_reviewer", "structure_reviewer", "rhetorical_analyst"}


def corpus(*payloads) -> list[Issue]:
    return [Issue.model_validate(payload) for payload in payloads]


def report_of(payload) -> ReviewerReport:
    return ReviewerReport.model_validate(payload.value)


# --------------------------------------------------------------------------- #
# Reviewer reports
# --------------------------------------------------------------------------- #


def test_clean_report_passes() -> None:
    outcome = check_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1")])),
        role_key="evidence_auditor",
        known_unit_ids=set(UNITS),
    )
    assert outcome.ok, outcome.problems


def test_wrong_reviewer_identity_is_rejected() -> None:
    outcome = check_reviewer_report(
        report_of(report("structure_reviewer", issues=[])), role_key="evidence_auditor"
    )
    assert not outcome.ok
    assert any("role key" in problem for problem in outcome.problems)


def test_duplicate_issue_ids_are_rejected() -> None:
    outcome = check_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1"), issue("ev-1")])),
        role_key="evidence_auditor",
    )
    assert any("duplicate issue ids" in problem for problem in outcome.problems)


def test_blocking_issue_with_weak_evidence_is_rejected() -> None:
    outcome = check_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1", evidence_status="uncertain")])),
        role_key="evidence_auditor",
    )
    assert any("cannot support a blocking finding" not in p and "evidence_status is uncertain" in p for p in outcome.problems)


def test_unknown_unit_reference_is_rejected() -> None:
    outcome = check_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1", unit_id="u9")])),
        role_key="evidence_auditor",
        known_unit_ids=set(UNITS),
    )
    assert any("unknown unit" in problem for problem in outcome.problems)


def test_normalisation_namespaces_ids_and_stamps_provenance() -> None:
    normalized, outcome = normalize_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1")])), role_key="evidence_auditor"
    )
    assert normalized.issues[0].id == "evidence_auditor:ev-1"
    assert normalized.issues[0].source_reviewer == "evidence_auditor"
    assert normalized.reviewer == "evidence_auditor"
    assert outcome.notes == []


def test_normalisation_downgrades_and_discloses() -> None:
    """A repair must move in the safe direction and be recorded, never silent."""
    normalized, outcome = normalize_reviewer_report(
        report_of(report("evidence_auditor", issues=[issue("ev-1", severity="critical", evidence_status="absent")])),
        role_key="evidence_auditor",
    )
    assert normalized.issues[0].severity is Severity.MINOR
    assert outcome.notes and "downgraded" in outcome.notes[0]
    # The finding is still visible: it was downgraded, not dropped.
    assert normalized.issues


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


def test_valid_plan_passes() -> None:
    outcome = validate_plan(
        ReviewPlan.model_validate(plan(specialists=["evidence_auditor", "structure_reviewer"]).value),
        registry_keys=REGISTRY_KEYS,
        write_capable_keys={"controlled_editor"},
    )
    assert outcome.ok, outcome.problems


@pytest.mark.parametrize(
    ("specialists", "units", "expected"),
    [
        ([], [unit("u1", "L1", "a")], "fewer than two specialists"),
        (["evidence_auditor"], [unit("u1", "L1", "a")], "fewer than two specialists"),
        (["evidence_auditor", "ghost"], [unit("u1", "L1", "a")], "unknown specialist"),
        (["evidence_auditor", "evidence_auditor"], [unit("u1", "L1", "a")], "more than once"),
        (["evidence_auditor", "structure_reviewer"], [unit("u1", "L1", "a"), unit("u1", "L2", "b")], "duplicate unit ids"),
        (["evidence_auditor", "structure_reviewer"], [], "no review units"),
    ],
)
def test_plans_are_rejected_for_the_right_reason(specialists, units, expected) -> None:
    payload = plan(specialists=specialists or ["x"], units=units or [unit("u1", "L1", "a")]).value
    payload["selected_specialists"] = specialists
    payload["units"] = units
    outcome = validate_plan(
        ReviewPlan.model_validate(payload), registry_keys=REGISTRY_KEYS, write_capable_keys={"controlled_editor"}
    )
    assert any(expected in problem for problem in outcome.problems), outcome.problems


def test_plan_may_not_assign_the_writer() -> None:
    outcome = validate_plan(
        ReviewPlan.model_validate(plan(specialists=["evidence_auditor", "controlled_editor"]).value),
        registry_keys=REGISTRY_KEYS,
        write_capable_keys={"controlled_editor"},
    )
    assert any("mutating roles" in problem for problem in outcome.problems)


# --------------------------------------------------------------------------- #
# Issue maps: the partition rules
# --------------------------------------------------------------------------- #


def base_corpus() -> list[Issue]:
    return corpus(issue("evidence_auditor:ev-1"), issue("structure_reviewer:st-1", severity="minor"))


def test_map_accepting_everything_passes() -> None:
    payload = issue_map(
        accepted=[resolution("evidence_auditor:ev-1"), resolution("structure_reviewer:st-1", action_required=False)],
        change_required=True,
    ).value
    outcome = validate_issue_map(
        type(issue_map().value) and __import__("reviewflow.models.contracts", fromlist=["ConsolidatedIssueMap"]).ConsolidatedIssueMap.model_validate(payload),
        corpus=base_corpus(),
        known_roles=REGISTRY_KEYS | {"controlled_editor"},
    )
    assert outcome.ok, outcome.problems


def test_map_may_not_invent_issue_ids() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(accepted=[resolution("evidence_auditor:ev-999")]).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert any("ids that no reviewer reported" in problem for problem in outcome.problems)


def test_map_may_not_drop_an_issue() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(accepted=[resolution("evidence_auditor:ev-1")]).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert any("left out of the map entirely" in problem for problem in outcome.problems)


def test_map_may_not_resolve_an_issue_twice() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(
        accepted=[resolution("evidence_auditor:ev-1"), resolution("structure_reviewer:st-1", action_required=False)],
        rejected=[resolution("structure_reviewer:st-1", disposition="rejected", action_required=False)],
    ).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert any("resolved more than once" in problem for problem in outcome.problems)


def test_merged_group_requires_a_resolved_canonical_id() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(
        accepted=[resolution("evidence_auditor:ev-1")],
        merged=[{"issue_ids": ["structure_reviewer:st-1", "evidence_auditor:ev-1"], "rationale": "same defect"}],
        change_required=True,
    ).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert any("canonical id" in problem for problem in outcome.problems)


def test_blocking_accepted_issue_must_require_an_action() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(
        accepted=[
            resolution("evidence_auditor:ev-1", action_required=False),
            resolution("structure_reviewer:st-1", action_required=False),
        ],
        change_required=False,
    ).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert any("has action_required=False" in problem for problem in outcome.problems)
    assert any("change_required is False" in problem for problem in outcome.problems)


def test_unadjudicated_conflict_is_disclosed_as_a_note() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    payload = issue_map(
        accepted=[resolution("evidence_auditor:ev-1"), resolution("structure_reviewer:st-1", action_required=False)],
        conflicts=[conflict(["evidence_auditor:ev-1"], adjudicated=False)],
        change_required=True,
    ).value
    outcome = validate_issue_map(
        ConsolidatedIssueMap.model_validate(payload), corpus=base_corpus(), known_roles=REGISTRY_KEYS
    )
    assert outcome.ok
    assert any("unadjudicated" in note for note in outcome.notes)


def test_mandatory_change_required_is_computed_by_code() -> None:
    from reviewflow.models.contracts import ConsolidatedIssueMap

    accepted = ConsolidatedIssueMap.model_validate(
        issue_map(accepted=[resolution("evidence_auditor:ev-1")]).value
    )
    only_minor = ConsolidatedIssueMap.model_validate(
        issue_map(
            accepted=[resolution("structure_reviewer:st-1", action_required=True), resolution("evidence_auditor:ev-1", action_required=False)],
            change_required=True,
        ).value
    )
    assert mandatory_change_required(accepted, base_corpus()) is True
    assert mandatory_change_required(only_minor, base_corpus()) is False


# --------------------------------------------------------------------------- #
# Edits: is the report consistent with the bytes?
# --------------------------------------------------------------------------- #

BEFORE = "the model reaches 97.3 percent"
AFTER = "the model reaches 97.3 percent (uncited)"
UNCHANGED = "the model reaches 97.3 percent"


def check_edit(**overrides):
    payload = edit_result(
        "manuscript.md",
        changes=[
            change("c1", issue_ids=["ev-1"], before=BEFORE, after=AFTER),
        ],
    ).value
    payload.update(overrides)
    from reviewflow.models.contracts import EditResult

    return EditResult.model_validate(payload)


def test_consistent_edit_report_passes() -> None:
    outcome = validate_edit_result(
        check_edit(),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
        text_before=BEFORE,
        text_after=AFTER,
    )
    assert outcome.ok, outcome.problems


def test_edit_report_must_match_the_granted_target() -> None:
    outcome = validate_edit_result(
        check_edit(target="other.md"),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
    )
    assert any("not the granted target" in problem for problem in outcome.problems)


def test_changes_outside_the_grant_are_rejected() -> None:
    outcome = validate_edit_result(
        check_edit(),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md", "other.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
    )
    assert any("outside the grant" in problem for problem in outcome.problems)


def test_file_changed_but_no_changes_reported_is_rejected() -> None:
    outcome = validate_edit_result(
        check_edit(changes=[]),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
    )
    assert any("reported no changes" in problem for problem in outcome.problems)


def test_no_change_requires_a_reason() -> None:
    outcome = validate_edit_result(
        check_edit(changes=[]),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="a" * 64,
        allowed_issue_ids={"ev-1"},
    )
    assert any("no_change_reason" in problem for problem in outcome.problems)


def test_reported_change_that_is_absent_from_the_result_is_rejected() -> None:
    """The check that catches an editor claiming an edit that never landed."""
    outcome = validate_edit_result(
        check_edit(),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
        text_before=BEFORE,
        text_after=UNCHANGED,  # the replacement text never appeared
    )
    assert any("the change was not applied" in problem for problem in outcome.problems)


def test_delete_must_name_text_that_existed() -> None:
    payload = edit_result(
        "manuscript.md", changes=[change("c1", issue_ids=["ev-1"], before="never present", after="", kind="delete")]
    ).value
    from reviewflow.models.contracts import EditResult

    outcome = validate_edit_result(
        EditResult.model_validate(payload),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
        text_before=BEFORE,
        text_after=BEFORE,
    )
    assert any("not present in the original" in problem for problem in outcome.problems)


def test_edit_citing_an_unaccepted_issue_is_rejected() -> None:
    payload = edit_result(
        "manuscript.md", changes=[change("c1", issue_ids=["ev-9"], before=BEFORE, after=AFTER)]
    ).value
    from reviewflow.models.contracts import EditResult

    outcome = validate_edit_result(
        EditResult.model_validate(payload),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1"},
    )
    assert any("outside the accepted set" in problem for problem in outcome.problems)


def test_unaddressed_accepted_issue_is_a_note_not_a_rejection() -> None:
    outcome = validate_edit_result(
        check_edit(),
        granted_target="manuscript.md",
        touched_paths=["manuscript.md"],
        sha_before="a" * 64,
        sha_after="b" * 64,
        allowed_issue_ids={"ev-1", "ev-2"},
        text_before=BEFORE,
        text_after=AFTER,
    )
    assert outcome.ok
    assert any("no change citing them" in note for note in outcome.notes)


# --------------------------------------------------------------------------- #
# Adversarial verdicts and verification reports
# --------------------------------------------------------------------------- #


def test_self_contradictory_verdict_is_rejected() -> None:
    payload = verdict(
        passed=True,
        critical=[{"description": "fabricated result", "location": "abstract", "evidence": "quote", "related_issue_id": None}],
    ).value
    outcome = validate_adversarial_verdict(
        AdversarialVerdict.model_validate(payload), allowed_issue_ids={"ev-1"}
    )
    assert any("self-contradictory" in problem for problem in outcome.problems)


def test_verdict_referencing_a_nonexistent_issue_is_rejected() -> None:
    payload = verdict(
        passed=False,
        critical=[
            {
                "description": "fabricated result",
                "location": "abstract",
                "evidence": "quote",
                "related_issue_id": "ghost-1",
            }
        ],
        rejection_reasons=["fabrication"],
    ).value
    outcome = validate_adversarial_verdict(
        AdversarialVerdict.model_validate(payload), allowed_issue_ids={"ev-1"}
    )
    assert any("do not exist" in problem for problem in outcome.problems)


def test_verdict_without_reasons_is_a_note() -> None:
    outcome = validate_adversarial_verdict(
        AdversarialVerdict(passed=False, rationale="it is wrong"), allowed_issue_ids=set()
    )
    assert outcome.ok
    assert any("no blocking reason" in note for note in outcome.notes)


def test_verification_without_a_mandatory_check_is_rejected() -> None:
    outcome = validate_verification_report(VerificationReport())
    assert any("no results" in problem for problem in outcome.problems)
    optional_only = VerificationReport(
        results=[
            VerificationResult(
                check="optional", command_or_method="m", passed=True, kind="deterministic", mandatory=False
            )
        ]
    )
    assert any("no mandatory check ran" in problem for problem in validate_verification_report(optional_only).problems)


def test_failed_check_without_evidence_is_a_note() -> None:
    report = VerificationReport(
        results=[
            VerificationResult(
                check="x", command_or_method="m", passed=False, evidence="", error=None, kind="deterministic", mandatory=True
            )
        ]
    )
    outcome = validate_verification_report(report)
    assert outcome.ok
    assert any("without evidence" in note for note in outcome.notes)


def test_adversarial_finding_model_is_strict() -> None:
    with pytest.raises(ValidationError):
        AdversarialFinding.model_validate({"description": "d", "location": "l"})
