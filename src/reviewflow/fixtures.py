"""Payload builders for scripted runs.

These construct the exact JSON a model would return for each contract, so tests
and evals can describe a scenario declaratively instead of hand-writing dicts.
They are deliberately dumb data builders: if a contract changes, mypy and the SDK
schema validation both catch the drift here.
"""

from __future__ import annotations

from typing import Any

from reviewflow.simulate import Structured, ToolCall, call

# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def unit(unit_id: str, location: str, description: str, priority: str = "major") -> dict[str, Any]:
    return {"unit_id": unit_id, "location": location, "description": description, "priority": priority}


def plan(
    *,
    specialists: list[str] | None = None,
    units: list[dict[str, Any]] | None = None,
    focus: list[str] | None = None,
    constraints: list[str] | None = None,
    objective: str = "review the target and fix the smallest set of well-evidenced issues",
    rationale: str = "two independent perspectives cover the target's evidence and coherence",
) -> Structured:
    return Structured(
        {
            "units": units
            or [
                unit("u1", "L1-L20", "opening section and headline claims", "critical"),
                unit("u2", "L21-L60", "body sections", "major"),
            ],
            "selected_specialists": specialists or ["evidence_auditor", "structure_reviewer"],
            "objective_restatement": objective,
            "focus": focus or [],
            "constraints": constraints or ["do not add new facts"],
            "rationale": rationale,
        }
    )


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


def issue(
    issue_id: str,
    *,
    severity: str = "critical",
    category: str = "unsupported_claim",
    description: str = "an assertion is presented without support",
    evidence: str = "quoted: 'our method reaches 97.3% answer accuracy'",
    evidence_status: str = "observed",
    recommendation: str = "cite the source or mark the figure as provisional",
    confidence: str = "high",
    location: str = "L1-L20",
    unit_id: str | None = "u1",
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "location": location,
        "category": category,
        "severity": severity,
        "description": description,
        "evidence": evidence,
        "evidence_status": evidence_status,
        "recommendation": recommendation,
        "confidence": confidence,
        "unit_id": unit_id,
    }


def report(
    reviewer: str,
    *,
    issues: list[dict[str, Any]] | None = None,
    assessment: str = "the material is coherent apart from the issues listed",
    scope: str = "assigned units",
    unresolved: list[str] | None = None,
) -> Structured:
    return Structured(
        {
            "reviewer": reviewer,
            "scope": scope,
            "issues": issues or [],
            "unresolved_questions": unresolved or [],
            "overall_assessment": assessment,
        }
    )


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


def resolution(
    issue_id: str,
    *,
    disposition: str = "accepted",
    rationale: str = "quoted directly from the material and load-bearing",
    action_required: bool = True,
    assigned_to: str = "controlled_editor",
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "disposition": disposition,
        "rationale": rationale,
        "action_required": action_required,
        "assigned_to": assigned_to,
    }


def conflict(issue_ids: list[str], *, adjudicated: bool = False, resolution_note: str | None = None) -> dict[str, Any]:
    return {
        "issue_ids": issue_ids,
        "summary": "reviewers disagree about whether this is a genuine defect",
        "reason": "the material alone cannot settle whether the figure is provisional or simply uncited",
        "adjudicated": adjudicated,
        "resolution_note": resolution_note,
    }


def issue_map(
    *,
    accepted: list[dict[str, Any]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
    merged: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    change_required: bool = True,
    change_rationale: str = "accepted issues require an edit",
) -> Structured:
    return Structured(
        {
            "accepted_issues": accepted or [],
            "rejected_issues": rejected or [],
            "merged_issues": merged or [],
            "conflicts": conflicts or [],
            "unresolved_questions": [],
            "change_required": change_required,
            "change_rationale": change_rationale,
        }
    )


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #


def change(
    change_id: str,
    *,
    issue_ids: list[str],
    before: str,
    after: str,
    kind: str = "replace",
    location: str = "L1-L20",
    rationale: str = "smallest defensible fix",
) -> dict[str, Any]:
    return {
        "change_id": change_id,
        "issue_ids": issue_ids,
        "kind": kind,
        "location": location,
        "before": before,
        "after": after,
        "rationale": rationale,
    }


def edit_result(
    target: str,
    *,
    changes: list[dict[str, Any]] | None = None,
    unresolved: list[str] | None = None,
    verification_requested: list[str] | None = None,
    no_change_reason: str | None = None,
) -> Structured:
    payload: dict[str, Any] = {
        "target": target,
        "changes": changes or [],
        "unresolved_items": unresolved or [],
        "verification_requested": verification_requested or [],
    }
    if no_change_reason is not None:
        payload["no_change_reason"] = no_change_reason
    return Structured(payload)


def apply_edit(
    *,
    path: str,
    old_text: str,
    new_text: str,
    issue_ids: list[str],
    rationale: str = "smallest defensible fix",
) -> ToolCall:
    """A real ``apply_edit`` tool call; the tool runs and the file really changes."""
    return call("apply_edit", path=path, old_text=old_text, new_text=new_text, issue_ids=issue_ids, rationale=rationale)


# --------------------------------------------------------------------------- #
# Post-edit review
# --------------------------------------------------------------------------- #


def diff_review(
    *,
    approved: bool = True,
    out_of_scope: list[str] | None = None,
    unsupported: list[str] | None = None,
    drift: list[str] | None = None,
    removed: list[str] | None = None,
    notes: list[str] | None = None,
) -> Structured:
    return Structured(
        {
            "reviewer": "diff_reviewer",
            "out_of_scope_changes": out_of_scope or [],
            "unsupported_assertions": unsupported or [],
            "removed_content_risks": removed or [],
            "semantic_drift": drift or [],
            "notes": notes or [],
            "approved": approved,
        }
    )


def finding(description: str, *, location: str = "abstract", evidence: str = "quoted text", issue_id: str | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "location": location,
        "evidence": evidence,
        "related_issue_id": issue_id,
    }


def verdict(
    *,
    passed: bool = True,
    critical: list[dict[str, Any]] | None = None,
    major: list[dict[str, Any]] | None = None,
    minor: list[dict[str, Any]] | None = None,
    rationale: str = "attempted to falsify the revision and found no blocking defect",
    rejection_reasons: list[str] | None = None,
) -> Structured:
    return Structured(
        {
            "passed": passed,
            "critical_issues": critical or [],
            "major_issues": major or [],
            "minor_issues": minor or [],
            "rationale": rationale,
            "rejection_reasons": rejection_reasons or [],
        }
    )


def semantic(name: str, *, passed: bool = True, evidence: str = "quoted from the revised text") -> dict[str, Any]:
    return {"check": name, "passed": passed, "method": "read the diff and the current text", "evidence": evidence}


def verification(
    *,
    passed: bool = True,
    overrides: dict[str, bool] | None = None,
    checks: list[str] | None = None,
) -> Structured:
    names = checks or ["edit_addresses_accepted_issues", "no_new_unsupported_claims", "uncertainty_preserved"]
    overrides = overrides or {}
    return Structured(
        {
            "results": [
                semantic(name, passed=overrides.get(name, passed), evidence="quoted from the material") for name in names
            ]
        }
    )


def final_report(
    *,
    claimed_status: str = "succeeded",
    summary: str = "the workflow completed and its gates were evaluated",
    accepted: list[str] | None = None,
    rejected: list[str] | None = None,
    disagreements: list[str] | None = None,
    changes: list[str] | None = None,
    unresolved: list[str] | None = None,
    verification_summary: list[str] | None = None,
    disclosures: list[str] | None = None,
) -> Structured:
    return Structured(
        {
            "executive_summary": summary,
            "accepted_issues_summary": accepted or [],
            "rejected_issues_summary": rejected or [],
            "reviewer_disagreements": disagreements or [],
            "changes_applied": changes or [],
            "unresolved_issues": unresolved or [],
            "verification_summary": verification_summary or [],
            "disclosures": disclosures or [],
            "claimed_status": claimed_status,
        }
    )
