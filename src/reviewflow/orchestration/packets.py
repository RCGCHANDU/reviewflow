"""Task packets: the only model-visible input the workflow constructs.

Design rules enforced here:

* a reviewer's packet contains the task, its own units, the material and shared
  constraints - and **nothing** from another reviewer, so independent judgement
  cannot anchor;
* the adversarial reviewer's packet contains the issue map and the diff, but not
  the diff reviewer's opinion and not the editor's self-assessment;
* every packet that quotes document text wraps it in the untrusted-material
  boundary, so the data/instruction distinction travels with the data;
* packets are built from typed objects, never by string-concatenating whatever an
  agent happened to return.
"""

from __future__ import annotations

import json

from reviewflow.context import WorkflowContext
from reviewflow.models.contracts import (
    AdversarialVerdict,
    ConsolidatedIssueMap,
    DiffReviewResult,
    Issue,
    ReviewerReport,
    ReviewUnit,
    VerificationReport,
    WorkflowResult,
)
from reviewflow.orchestration.ingest import IngestResult
from reviewflow.prompts import untrusted_material_block

#: Appended to any packet that asks a reviewer to judge an edited artifact.
DIFF_PREAMBLE = "The following unified diff was computed by the workflow from the real bytes, not reported by an agent."


def _json_block(title: str, payload: object) -> str:
    body = json.dumps(payload, indent=2, default=str)
    return f"{title}\n```json\n{body}\n```"


def _units_for(plan_units: list[ReviewUnit], assigned: list[str]) -> list[dict[str, str]]:
    wanted = set(assigned)
    return [
        {"unit_id": u.unit_id, "location": u.location, "description": u.description, "priority": u.priority.value}
        for u in plan_units
        if u.unit_id in wanted
    ]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def plan_packet(context: WorkflowContext, ingest: IngestResult) -> str:
    hints = [{"location": h.location, "description": h.description, "priority": h.priority} for h in ingest.unit_hints]
    parts = [
        "# Task",
        f"Objective: {context.objective}",
        f"Target: {ingest.relative_path} ({ingest.line_count} lines, {ingest.char_count} characters)",
        "Immutable constraints:",
        *[f"- {c}" for c in context.constraints],
        "",
        _json_block("Structural hints extracted from the target (verify them yourself):", hints),
        "",
        "Produce the review plan: units covering the whole target, and the specialists to run.",
        "",
        untrusted_material_block(ingest.relative_path, ingest.numbered()),
    ]
    if ingest.warnings:
        parts.insert(4, "Ingest warnings:\n" + "\n".join(f"- {w}" for w in ingest.warnings))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Parallel review
# --------------------------------------------------------------------------- #


def review_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    units: list[ReviewUnit],
    assigned_unit_ids: list[str],
    focus: list[str],
    round_index: int = 0,
) -> str:
    """One reviewer's independent packet.

    Contains no other reviewer's output. This is what makes the reviews
    independent rather than merely parallel.
    """
    parts = [
        "# Task",
        f"Objective: {context.objective}",
        f"Target: {ingest.relative_path}",
        f"Round: {round_index}",
        "",
        _json_block("Your assigned review units:", _units_for(units, assigned_unit_ids)),
        "",
        "Shared constraints you must respect:",
        *[f"- {c}" for c in context.constraints],
    ]
    if focus:
        parts.extend(["", "Focus requested by the plan:", *[f"- {f}" for f in focus]])
    parts.extend(
        [
            "",
            "Report every concrete defect you find with a verbatim quote. If your units are sound, "
            "return an empty issues list - do not invent work.",
            "",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


def synthesis_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    reports: list[ReviewerReport],
    round_index: int,
    unresolved_findings: list[str] | None = None,
    prior_map: ConsolidatedIssueMap | None = None,
) -> str:
    corpus = _corpus_payload(reports)
    parts = [
        "# Task",
        f"Objective: {context.objective}",
        f"Target: {ingest.relative_path}",
        f"Round: {round_index}",
        "",
        "Reconcile the reviewer reports below into one issue map. You are the only agent that sees "
        "all of them. Every issue id must be resolved exactly once, and you may not invent ids.",
        "",
        _json_block("Reviewer reports (each is one independent reviewer's full output):", corpus),
    ]
    if prior_map is not None:
        parts.extend(
            [
                "",
                _json_block("Your issue map from the previous round (for continuity):", prior_map.model_dump(mode="json")),
            ]
        )
    if unresolved_findings:
        parts.extend(
            [
                "",
                "Findings raised after the previous edit that are still unresolved. Decide whether each "
                "requires a further action, and say so through the accepted issues and change_rationale:",
                *[f"- {f}" for f in unresolved_findings],
            ]
        )
    parts.extend(
        [
            "",
            "If reviewers disagree in a way the evidence cannot settle, record it in conflicts with "
            "adjudicated=false and disclose it. Do not smooth disagreements away.",
            "",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )
    return "\n".join(parts)


def _corpus_payload(reports: list[ReviewerReport]) -> list[dict[str, object]]:
    return [
        {
            "reviewer": report.reviewer,
            "scope": report.scope,
            "overall_assessment": report.overall_assessment,
            "unresolved_questions": report.unresolved_questions,
            "coverage_notes": report.coverage_notes,
            "issues": [issue.model_dump(mode="json") for issue in report.issues],
        }
        for report in reports
    ]


# --------------------------------------------------------------------------- #
# Controlled edit
# --------------------------------------------------------------------------- #


def edit_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    issue_map: ConsolidatedIssueMap,
    corpus: list[Issue],
    unresolved_findings: list[str] | None = None,
    round_index: int = 0,
) -> str:
    actionable = _actionable(issue_map, corpus)
    parts = [
        "# Task",
        f"Objective: {context.objective}",
        f"Round: {round_index}",
        "Apply the smallest defensible edit that resolves the accepted, action-required issues below.",
        "Every other accepted issue must appear in unresolved_items with a reason.",
        "",
        _json_block("Accepted issues requiring an action:", actionable),
        "",
        _json_block(
            "Rejected issues (do not act on these):",
            [r.model_dump(mode="json") for r in issue_map.rejected_issues],
        ),
    ]
    if issue_map.conflicts:
        parts.extend(
            [
                "",
                _json_block("Unresolved reviewer conflicts (keep both readings visible in your report):",
                           [c.model_dump(mode="json") for c in issue_map.conflicts]),
            ]
        )
    if unresolved_findings:
        parts.extend(["", "Also still unresolved from the previous round:", *[f"- {f}" for f in unresolved_findings]])
    parts.extend(
        [
            "",
            f"Change rationale from synthesis: {issue_map.change_rationale}",
            "",
            "Constraints: no new facts, no strengthened claims, no removed caveats, no unrelated edits.",
            "",
            "Quoting rule: the `N|` prefixes below are line numbers added for reference. They are NOT "
            "part of the file. Copy `old_text` from the material without those prefixes, and match "
            "wrapping: a phrase that spans two lines in the file contains a newline.",
            "",
            "Current target contents (edit only within the granted line budget):",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )
    return "\n".join(parts)


def _actionable(issue_map: ConsolidatedIssueMap, corpus: list[Issue]) -> list[dict[str, object]]:
    by_id = {issue.id: issue for issue in corpus}
    payload: list[dict[str, object]] = []
    for resolution in issue_map.accepted_issues:
        issue = by_id.get(resolution.issue_id)
        if issue is None:
            continue
        if not resolution.action_required:
            continue
        payload.append(
            {
                "issue_id": issue.id,
                "severity": issue.severity.value,
                "category": issue.category.value,
                "location": issue.location,
                "description": issue.description,
                "evidence": issue.evidence,
                "recommendation": issue.recommendation,
                "synthesis_rationale": resolution.rationale,
            }
        )
    return payload


# --------------------------------------------------------------------------- #
# Diff review and adversarial review
# --------------------------------------------------------------------------- #


def diff_review_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    diff_text: str,
    stats: dict[str, object],
    issue_map: ConsolidatedIssueMap,
    corpus: list[Issue],
    round_index: int,
) -> str:
    return "\n".join(
        [
            "# Task",
            f"Round: {round_index}. Judge whether the edit stayed inside the accepted scope.",
            "",
            DIFF_PREAMBLE,
            _json_block("Diff statistics computed by code:", stats),
            "",
            _json_block("Accepted issues the edit was supposed to resolve:", _actionable(issue_map, corpus)),
            "",
            "```diff",
            diff_text,
            "```",
            "",
            "Report out-of-scope changes, new unsupported assertions, removed meaning and semantic drift.",
            "",
            "Material after the edit:",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )


def adversarial_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    diff_text: str,
    issue_map: ConsolidatedIssueMap,
    corpus: list[Issue],
    round_index: int,
    unresolved_findings: list[str] | None = None,
    no_change: bool = False,
) -> str:
    """The adversarial reviewer sees the diff and the issue map, and no peer verdicts.

    When no edit was applied the packet reframes the task: the claim under attack
    is then "no change is the right disposition", which is a real failure mode
    (reviewers missed something that required an edit).
    """
    if no_change:
        headline = (
            f"# Task\nRound {round_index}. No edit was applied. Attack that decision: find a concrete defect "
            "in the material that requires an edit and that the review missed. If the material genuinely needs "
            "no change, say so - but only after trying to find one. Your default posture is rejection."
        )
    else:
        headline = (
            f"# Task\nRound {round_index}. Attempt to reject this revision. Your default posture is rejection."
        )
    parts = [
        headline,
        "",
        "You are not shown the diff reviewer's verdict, the editor's self-assessment, or any other "
        "agent's conclusion, deliberately: your job is independent falsification.",
        "",
    ]
    if not no_change:
        parts.extend([DIFF_PREAMBLE, "```diff", diff_text, "```", ""])
    parts.extend(
        [
            _json_block(
                "Accepted issues the disposition claims to resolve (verify each actually holds):",
                _actionable(issue_map, corpus),
            )
        ]
    )
    if issue_map.conflicts:
        parts.extend(["", _json_block("Known reviewer conflicts:", [c.model_dump(mode="json") for c in issue_map.conflicts])])
    if unresolved_findings:
        parts.extend(["", "Still-open findings from earlier rounds:", *[f"- {f}" for f in unresolved_findings]])
    parts.extend(
        [
            "",
            "Revised material:" if not no_change else "Material:",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )
    return "\n".join(parts)



# --------------------------------------------------------------------------- #
# Final report
# --------------------------------------------------------------------------- #


def final_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    issue_map: ConsolidatedIssueMap | None,
    corpus: list[Issue],
    verification: VerificationReport | None,
    adversarial: AdversarialVerdict | None,
    diff_review: DiffReviewResult | None,
    diff_text: str,
    computed_status: str,
    status_reason: str,
    failures: list[str],
    rounds_executed: int,
    coverage: dict[str, list[str]],
) -> str:
    """Everything the final report may draw on - all of it recorded in the ledger."""
    parts = [
        "# Task",
        f"Objective: {context.objective}",
        f"Target: {ingest.relative_path}",
        f"Rounds executed: {rounds_executed}",
        "",
        "Write the final user-facing report. You may not claim a stronger status than the engine "
        "computed, and you must keep disagreements, failures and open issues visible.",
        "",
        f"Engine-computed status: {computed_status}",
        f"Engine reason: {status_reason}",
        "",
        _json_block("Review coverage:", coverage),
    ]
    if issue_map is not None:
        parts.extend(
            [
                "",
                _json_block("Final issue map:", issue_map.model_dump(mode="json")),
                "",
                _json_block("Issues behind the map (for accurate summaries):",
                           [i.model_dump(mode="json") for i in corpus]),
            ]
        )
    if verification is not None:
        parts.extend(["", _json_block("Verification results:", [r.model_dump(mode="json") for r in verification.results])])
    if adversarial is not None:
        parts.extend(["", _json_block("Adversarial verdict (final round):", adversarial.model_dump(mode="json"))])
    if diff_review is not None:
        parts.extend(["", _json_block("Diff review (final round):", diff_review.model_dump(mode="json"))])
    if diff_text.strip():
        parts.extend(["", "Actual diff applied:", "```diff", diff_text, "```"])
    if failures:
        parts.extend(["", "Failures that must be disclosed:", *[f"- {f}" for f in failures]])
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verification_packet(
    context: WorkflowContext,
    *,
    ingest: IngestResult,
    checks: list[dict[str, str]],
    issue_map: ConsolidatedIssueMap | None,
    corpus: list[Issue],
    diff_text: str,
) -> str:
    payload = {
        "checks": checks,
        "accepted_issues": _actionable(issue_map, corpus) if issue_map else [],
    }
    return "\n".join(
        [
            "# Task",
            "Perform exactly the semantic checks requested below against the CURRENT bytes on disk.",
            "You may not answer from memory of an earlier revision.",
            "",
            _json_block("Requested checks:", payload),
            "",
            "```diff",
            diff_text or "(no change was applied)",
            "```",
            "",
            untrusted_material_block(ingest.relative_path, ingest.numbered()),
        ]
    )


def result_packet(result: WorkflowResult) -> str:
    """Compact JSON view of a workflow result, used by the CLI and evals."""
    return json.dumps(result.model_dump(mode="json"), indent=2, default=str)
