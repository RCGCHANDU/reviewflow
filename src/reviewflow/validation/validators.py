"""Application-level validators.

These are the checks that SDK guardrails cannot do: SDK input/output guardrails
only run at workflow boundaries, so every *intermediate* specialist output is
validated here before it is allowed to influence a state transition.

Design rule: validators do not raise and do not silently coerce. Each one
reports **problems** (which the engine turns into a bounded retry) and **notes**
(disclosed repairs, surfaced in the final report). A repair is only acceptable
when it moves in the safe direction and is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewflow.models.contracts import (
    AdversarialVerdict,
    ConsolidatedIssueMap,
    EditResult,
    Issue,
    ReviewerReport,
    ReviewPlan,
    VerificationReport,
)
from reviewflow.models.enums import (
    BLOCKING_SEVERITIES,
    GROUNDING_EVIDENCE_STATUSES,
    ChangeKind,
    EvidenceStatus,
    Severity,
    is_blocking,
)


@dataclass
class ValidationOutcome:
    """Result of validating one agent output."""

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def extend(self, other: ValidationOutcome) -> ValidationOutcome:
        self.problems.extend(other.problems)
        self.notes.extend(other.notes)
        return self

    def as_feedback(self) -> str:
        """Corrective feedback appended to a retry prompt."""
        return "\n".join(f"- {p}" for p in self.problems)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def validate_plan(plan: ReviewPlan, *, registry_keys: set[str], write_capable_keys: set[str]) -> ValidationOutcome:
    """A plan is only usable if it decomposes the target and names real, read-only specialists."""
    out = ValidationOutcome()
    if not plan.units:
        out.problems.append("plan contains no review units")
    unit_ids = [u.unit_id for u in plan.units]
    duplicates = sorted({u for u in unit_ids if unit_ids.count(u) > 1})
    if duplicates:
        out.problems.append(f"duplicate unit ids: {duplicates}")

    if len(plan.selected_specialists) < 2:
        out.problems.append("fewer than two specialists selected; independent review requires at least two")
    repeated = sorted({s for s in plan.selected_specialists if plan.selected_specialists.count(s) > 1})
    if repeated:
        out.problems.append(f"specialist listed more than once: {repeated}")
    unknown = sorted(set(plan.selected_specialists) - registry_keys)
    if unknown:
        out.problems.append(f"unknown specialist role keys: {unknown}")
    writers = sorted(set(plan.selected_specialists) & write_capable_keys)
    if writers:
        out.problems.append(f"plan tries to assign mutating roles to the read-only review stage: {writers}")
    if not plan.objective_restatement.strip():
        out.problems.append("objective_restatement is empty")
    return out


# --------------------------------------------------------------------------- #
# Reviewer reports
# --------------------------------------------------------------------------- #


def check_reviewer_report(
    report: ReviewerReport,
    *,
    role_key: str,
    known_unit_ids: set[str] | None = None,
) -> ValidationOutcome:
    """Semantic checks on a reviewer report. Returns problems only; no mutation."""
    out = ValidationOutcome()
    if report.reviewer != role_key:
        out.problems.append(
            f"report.reviewer is {report.reviewer!r} but this agent's role key is {role_key!r}"
        )
    ids = [issue.id for issue in report.issues]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        out.problems.append(f"duplicate issue ids inside one report: {duplicates}")
    for issue in report.issues:
        if issue.severity in BLOCKING_SEVERITIES and issue.evidence_status not in GROUNDING_EVIDENCE_STATUSES:
            out.problems.append(
                f"issue {issue.id} is {issue.severity.value} but its evidence_status is "
                f"{issue.evidence_status.value}; blocking issues need observed, inferred or contradicted evidence"
            )
        if known_unit_ids and issue.unit_id and issue.unit_id not in known_unit_ids:
            out.problems.append(f"issue {issue.id} references unknown unit {issue.unit_id!r}")
        if known_unit_ids and _looks_like_unit_token(issue.location) and issue.location not in known_unit_ids:
            out.problems.append(f"issue {issue.id} references unknown unit id {issue.location!r} in location")
    return out


def normalize_reviewer_report(report: ReviewerReport, *, role_key: str) -> tuple[ReviewerReport, ValidationOutcome]:
    """Apply disclosed, safety-preserving repairs and return the usable report.

    Repairs are strictly conservative:

    * issue ids are namespaced with the role key (global uniqueness for synthesis);
    * ``source_reviewer`` is stamped (code-added provenance);
    * an unevidenced blocking issue is *downgraded* to minor with a recorded note
      rather than dropped, so the finding stays visible;
    * an unknown ``unit_id`` is cleared rather than guessed.
    """
    out = ValidationOutcome()
    namespaced: list[Issue] = []
    for issue in report.issues:
        unit_id = issue.unit_id
        if unit_id is not None and not unit_id.strip():
            unit_id = None
        severity = issue.severity
        if severity in BLOCKING_SEVERITIES and issue.evidence_status not in GROUNDING_EVIDENCE_STATUSES:
            severity = Severity.MINOR
            out.notes.append(
                f"issue {issue.id} downgraded from {issue.severity.value} to minor: "
                f"evidence_status={issue.evidence_status.value} cannot support a blocking finding"
            )
        namespaced.append(
            issue.model_copy(
                update={"id": f"{role_key}:{issue.id}", "source_reviewer": role_key, "unit_id": unit_id, "severity": severity}
            )
        )
    return report.model_copy(update={"reviewer": role_key, "issues": namespaced}), out


def _looks_like_unit_token(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.startswith("u") and stripped[1:].isdigit()


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


def validate_issue_map(
    issue_map: ConsolidatedIssueMap,
    *,
    corpus: list[Issue],
    known_roles: set[str] | None = None,
) -> ValidationOutcome:
    """The issue map must be a complete, unambiguous partition of the corpus.

    This is the anti-hallucination and anti-silent-drop gate: synthesis may not
    invent issue ids, and it may not lose one either.
    """
    out = ValidationOutcome()
    corpus_ids = {issue.id for issue in corpus}
    accepted = list(issue_map.accepted_issues)
    rejected = issue_map.rejected_issues
    accepted_ids = [r.issue_id for r in accepted]
    rejected_ids = [r.issue_id for r in rejected]

    for label, ids in (("accepted", accepted_ids), ("rejected", rejected_ids)):
        unknown = sorted(set(ids) - corpus_ids)
        if unknown:
            out.problems.append(f"{label}_issues reference ids that no reviewer reported: {unknown}")
    duplicates = sorted({i for i in accepted_ids + rejected_ids if (accepted_ids + rejected_ids).count(i) > 1})
    if duplicates:
        out.problems.append(f"ids resolved more than once: {duplicates}")
    overlap = sorted(set(accepted_ids) & set(rejected_ids))
    if overlap:
        out.problems.append(f"ids both accepted and rejected: {overlap}")

    merged_members: list[str] = []
    for group in issue_map.merged_issues:
        if len(group.issue_ids) < 2:
            out.problems.append(f"merged group {group.issue_ids} has fewer than two members")
        unknown = sorted(set(group.issue_ids) - corpus_ids)
        if unknown:
            out.problems.append(f"merged group references unknown ids: {unknown}")
        merged_members.extend(group.issue_ids[1:])
        canonical = group.issue_ids[0]
        if canonical not in accepted_ids and canonical not in rejected_ids:
            out.problems.append(
                f"merged group canonical id {canonical!r} (first element) is not resolved as accepted or rejected"
            )
    duplicate_members = sorted({m for m in merged_members if merged_members.count(m) > 1})
    if duplicate_members:
        out.problems.append(f"ids appear in more than one merged group: {duplicate_members}")
    listed_as_resolved = sorted(set(merged_members) & (set(accepted_ids) | set(rejected_ids)))
    if listed_as_resolved:
        out.problems.append(
            f"non-canonical merged ids must not have their own resolution: {listed_as_resolved}"
        )

    missing = sorted(corpus_ids - (set(accepted_ids) | set(rejected_ids) | set(merged_members)))
    if missing:
        out.problems.append(f"issues left out of the map entirely: {missing}")

    severity_by_id = {issue.id: issue.severity for issue in corpus}
    blocking_accepted = [
        r.issue_id for r in accepted if is_blocking(severity_by_id.get(r.issue_id, Severity.INFO))
    ]
    for resolution in accepted:
        severity = severity_by_id.get(resolution.issue_id)
        if severity is not None and is_blocking(severity) and not resolution.action_required:
            out.problems.append(
                f"accepted {severity.value} issue {resolution.issue_id} has action_required=False; "
                "blocking findings must require an action"
            )
    if blocking_accepted and not issue_map.change_required:
        out.problems.append(
            f"change_required is False but blocking issues were accepted: {sorted(blocking_accepted)}"
        )
    if not blocking_accepted and issue_map.change_required:
        out.notes.append("change_required is True although no blocking issue was accepted")

    if known_roles:
        for resolution in accepted:
            if resolution.assigned_to not in known_roles and resolution.assigned_to != "orchestrator":
                out.notes.append(
                    f"issue {resolution.issue_id} is assigned to unknown role {resolution.assigned_to!r}"
                )
    unresolved = [c for c in issue_map.conflicts if not c.adjudicated]
    if unresolved:
        out.notes.append(f"{len(unresolved)} reviewer conflict(s) left unadjudicated and disclosed")
    return out


def mandatory_change_required(issue_map: ConsolidatedIssueMap, corpus: list[Issue]) -> bool:
    """Code's own answer to 'must something change?', independent of the model."""
    blockers = {issue.id for issue in corpus if is_blocking(issue.severity)}
    return bool(set(issue_map.accepted_ids()) & blockers)


# --------------------------------------------------------------------------- #
# Controlled edit
# --------------------------------------------------------------------------- #


def validate_edit_result(
    edit: EditResult,
    *,
    granted_target: str,
    touched_paths: list[str],
    sha_before: str,
    sha_after: str,
    allowed_issue_ids: set[str],
    text_before: str = "",
    text_after: str = "",
) -> ValidationOutcome:
    """Verify the editor's report against the *observed* filesystem facts."""
    out = ValidationOutcome()
    if edit.target != granted_target:
        out.problems.append(f"edit.target {edit.target!r} is not the granted target {granted_target!r}")
    outside = sorted(set(touched_paths) - {granted_target})
    if outside:
        out.problems.append(f"files changed outside the grant: {outside}")

    changed = sha_before != sha_after
    if changed and not edit.changes:
        out.problems.append("the target changed on disk but the editor reported no changes")
    if not changed:
        if edit.changes:
            out.problems.append("the editor reported changes but the target is byte-identical")
        if not (edit.no_change_reason or "").strip():
            out.problems.append("no change was applied and no_change_reason was not provided")

    cited = {issue_id for change in edit.changes for issue_id in change.issue_ids}
    unknown = sorted(cited - allowed_issue_ids)
    if unknown:
        out.problems.append(f"changes cite issue ids outside the accepted set: {unknown}")

    # A reported change must be visible in the real revisions. Without this, an editor
    # could claim an edit that a rejected tool call never applied.
    if changed and text_before and text_after:
        for entry in edit.changes:
            if entry.kind is ChangeKind.DELETE:
                if entry.before and entry.before not in text_before:
                    out.problems.append(
                        f"change {entry.change_id} claims to delete text that was not present in the original"
                    )
                continue
            if entry.after and entry.after not in text_after:
                out.problems.append(
                    f"change {entry.change_id} reports text that is absent from the edited file; "
                    "the change was not applied"
                )
            if entry.before and entry.kind is ChangeKind.REPLACE and entry.before not in text_before:
                out.problems.append(
                    f"change {entry.change_id} reports removing text that was not present in the original"
                )
    unaddressed = sorted(allowed_issue_ids - cited)
    if changed and unaddressed:
        out.notes.append(f"accepted issues with no change citing them: {unaddressed}")
    return out


# --------------------------------------------------------------------------- #
# Adversarial review
# --------------------------------------------------------------------------- #


def validate_adversarial_verdict(verdict: AdversarialVerdict, *, allowed_issue_ids: set[str]) -> ValidationOutcome:
    out = ValidationOutcome()
    if verdict.passed and (verdict.critical_issues or verdict.major_issues):
        out.problems.append(
            "verdict.passed is True while critical/major findings are listed; the verdict is self-contradictory"
        )
    if not verdict.passed and not verdict.rejection_reasons and not (verdict.critical_issues or verdict.major_issues):
        out.notes.append("verdict is a rejection but lists no blocking reason")
    bogus = sorted(
        {
            f.related_issue_id
            for f in [*verdict.critical_issues, *verdict.major_issues, *verdict.minor_issues]
            if f.related_issue_id and f.related_issue_id not in allowed_issue_ids
        }
    )
    if bogus:
        out.problems.append(f"findings reference issue ids that do not exist: {bogus}")
    return out


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def validate_verification_report(report: VerificationReport) -> ValidationOutcome:
    out = ValidationOutcome()
    if not report.results:
        out.problems.append("verification produced no results; an unverified workflow cannot complete")
    if not any(r.mandatory for r in report.results):
        out.problems.append("no mandatory check ran; success would be unverifiable")
    for result in report.results:
        if not result.passed and not result.error and not result.evidence:
            out.notes.append(f"check {result.check!r} failed without evidence or error text")
    return out


def evidence_summary(report: ReviewerReport) -> dict[str, int]:
    """Small helper used by evals and the final report: counts by severity."""
    counts: dict[str, int] = {s.value: 0 for s in Severity}
    for issue in report.issues:
        counts[issue.severity.value] += 1
    return counts


def unsupported_evidence_ratio(report: ReviewerReport) -> float:
    """Fraction of issues whose evidence is absent/uncertain. Used as an eval metric."""
    if not report.issues:
        return 0.0
    weak = sum(
        1
        for i in report.issues
        if i.evidence_status in {EvidenceStatus.ABSENT, EvidenceStatus.UNCERTAIN}
    )
    return weak / len(report.issues)
