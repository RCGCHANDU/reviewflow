"""Structured contracts and prompt architecture.

Two guarantees are tested here:

1. every contract used as an ``output_type`` is a strict Pydantic schema - extra
   fields rejected, enums enumerated;
2. the *prompt* describing a contract is generated from that same schema, so a
   prompt can never describe a field the validator does not enforce.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewflow.agents.registry import build_default_registry
from reviewflow.fixtures import issue, report
from reviewflow.models.contracts import (
    AdversarialVerdict,
    ConsolidatedIssueMap,
    EditResult,
    FinalReport,
    Issue,
    ReviewerReport,
    ReviewPlan,
    VerificationReport,
    VerificationResult,
    WorkflowResult,
)
from reviewflow.models.enums import Severity, Stage, WorkflowStatus, is_blocking, most_severe
from reviewflow.prompts import ROLE_SPECS, UNTRUSTED_DATA_POLICY, compose_role_prompt, render_output_contract

OUTPUT_CONTRACTS = [
    ReviewPlan,
    ReviewerReport,
    ConsolidatedIssueMap,
    EditResult,
    AdversarialVerdict,
    FinalReport,
]


def test_every_output_contract_is_strict() -> None:
    """`extra="forbid"` means a drifting model fails loudly instead of silently."""
    for model in OUTPUT_CONTRACTS:
        assert model.model_config.get("extra") == "forbid", model.__name__
        schema = model.model_json_schema()
        assert schema.get("additionalProperties") is False, model.__name__


def test_contracts_are_json_serialisable_with_enums() -> None:
    payload = report("evidence_auditor", issues=[issue("ev-1")]).value
    parsed = ReviewerReport.model_validate(payload)
    assert parsed.reviewer == "evidence_auditor"
    assert parsed.issues[0].severity is Severity.CRITICAL
    assert parsed.model_dump(mode="json")["issues"][0]["severity"] == "critical"


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewerReport.model_validate({"reviewer": "r", "scope": "s", "overall_assessment": "a", "verdict": "good"})


def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Issue.model_validate({"id": "x", "location": "L1", "category": "clarity", "severity": "minor"})


def test_enum_values_are_enforced() -> None:
    with pytest.raises(ValidationError):
        Issue.model_validate(
            {
                "id": "x",
                "location": "L1",
                "category": "clarity",
                "severity": "blocker",  # not a severity
                "description": "d",
                "evidence": "e",
                "evidence_status": "observed",
                "recommendation": "r",
                "confidence": "high",
            }
        )


def test_render_output_contract_lists_every_field_and_enum() -> None:
    """Prompt/schema drift guard: every property and enum value appears in the prompt text."""
    for model in OUTPUT_CONTRACTS:
        text = render_output_contract(model)
        schema = model.model_json_schema()
        for field_name in schema.get("properties", {}):
            assert f"`{field_name}`" in text, f"{model.__name__}.{field_name} missing from the rendered contract"
        assert model.__name__ in text


def test_output_contract_names_enum_values_for_critical_fields() -> None:
    text = render_output_contract(ReviewerReport)
    assert "critical" in text and "major" in text and "minor" in text
    assert "observed" in text and "absent" in text


def test_role_prompts_contain_all_required_blocks() -> None:
    for role_key, spec in ROLE_SPECS.items():
        text = compose_role_prompt(spec, output_contract="CONTRACT", runtime_context="RUNTIME")
        for block in (
            "## 1. Identity",
            "## 2. Objective",
            "## 3. Scope",
            "## 4. Non-goals",
            "## 5. Authority",
            "## 6. Evidence policy",
            "## 7. Decision criteria",
            "## 8. Uncertainty policy",
            "## 9. Output contract",
            "## 10. Completion criteria",
        ):
            assert block in text, f"{role_key} is missing {block}"
        assert UNTRUSTED_DATA_POLICY.splitlines()[0] in text, f"{role_key} is missing the untrusted-data policy"


def test_read_only_roles_state_they_cannot_write() -> None:
    for role_key in ("evidence_auditor", "technical_reviewer", "structure_reviewer", "rhetorical_analyst"):
        assert "no tool that can" in ROLE_SPECS[role_key].authority.replace("\n", " ")


def test_prompts_do_not_contain_credentials() -> None:
    """The runtime context and prompts must never carry secrets."""
    for role_key, spec in ROLE_SPECS.items():
        text = compose_role_prompt(spec, output_contract="c", runtime_context="r")
        for marker in ("sk-", "AKIA", "Bearer "):
            assert marker not in text, f"{role_key} prompt contains {marker}"


def test_severity_ordering_helpers() -> None:
    assert is_blocking(Severity.CRITICAL) and is_blocking(Severity.MAJOR)
    assert not is_blocking(Severity.MINOR) and not is_blocking(Severity.INFO)
    assert most_severe([Severity.MINOR, Severity.CRITICAL, Severity.MAJOR]) is Severity.CRITICAL
    assert most_severe([]) is None


def test_verification_report_requires_a_mandatory_check() -> None:
    empty = VerificationReport()
    assert not empty.all_mandatory_passed
    passed = VerificationReport(
        results=[
            VerificationResult(
                check="x", command_or_method="m", passed=True, evidence="e", kind="deterministic", mandatory=True
            )
        ]
    )
    assert passed.all_mandatory_passed
    failed = VerificationReport(
        results=[
            VerificationResult(
                check="x", command_or_method="m", passed=False, evidence="", kind="deterministic", mandatory=True
            )
        ]
    )
    assert not failed.all_mandatory_passed
    assert [r.check for r in failed.mandatory_failures] == ["x"]


def test_workflow_result_ok_is_only_true_for_succeeded() -> None:
    for status in WorkflowStatus:
        result = WorkflowResult(
            workflow_id="wf", status=status, status_reason="r", stages_completed=[Stage.COMPLETE]
        )
        assert result.ok is (status is WorkflowStatus.SUCCEEDED)


def test_default_registry_has_exactly_one_writer() -> None:
    registry = build_default_registry()
    assert len(registry.writers()) == 1
    assert registry.writer().role_key == "controlled_editor"
    assert registry.writer().may_mutate
    assert not any(spec.may_mutate for spec in registry.planner_selectable())
