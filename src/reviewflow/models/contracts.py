"""Structured data contracts exchanged between workflow stages.

Every contract here is a Pydantic model, because the Agents SDK turns an
``Agent.output_type`` into a strict JSON schema for the model and validates the
response before returning it. Field descriptions are therefore *load-bearing*:
they are the output contract the model actually reads, so they are written as
instructions rather than as prose documentation.

Code-added fields (as opposed to model-authored fields) are marked in their
description as "CODE-ADDED"; the workflow fills them in after validation and
never trusts a model to supply them.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from reviewflow.models.enums import (
    ChangeKind,
    Confidence,
    Disposition,
    EvidenceStatus,
    IssueCategory,
    Severity,
    Stage,
    VerificationKind,
    WorkflowStatus,
)

NonEmptyStr = Annotated[str, Field(min_length=1)]


class _Strict(BaseModel):
    """Base class that rejects unknown fields so a drifting prompt fails loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Review contracts (read-only specialists)
# --------------------------------------------------------------------------- #


class Issue(_Strict):
    """One concrete defect found by a reviewer."""

    id: NonEmptyStr = Field(
        description="Stable short id, unique inside your report only, e.g. 'ev-1'. Use a role prefix."
    )
    location: NonEmptyStr = Field(
        description=(
            "Where the issue is: a unit id from your assigned units, a heading, a line range, "
            "or 'global' for whole-artifact issues."
        )
    )
    category: IssueCategory = Field(description="Best-fitting category from the allowed set.")
    severity: Severity = Field(
        description=(
            "critical = invalidates a claim/conclusion or blocks reuse; major = materially weak "
            "reasoning, unsupported but recoverable; minor = clarity/consistency; info = optional."
        )
    )
    description: NonEmptyStr = Field(description="The defect, stated so an editor can act on it.")
    evidence: NonEmptyStr = Field(
        description=(
            "The exact quoted text, missing citation, numeric mismatch, or line reference that "
            "demonstrates the defect. Quote when possible. Never leave this empty."
        )
    )
    evidence_status: EvidenceStatus = Field(
        description=(
            "observed = you read it in the material; inferred = logically derived from it; "
            "interpretation = a reading; uncertain = you could not decide; contradicted = the "
            "material conflicts elsewhere; absent = you found no support at all. "
            "critical/major issues require observed, inferred or contradicted."
        )
    )
    recommendation: NonEmptyStr = Field(
        description="The smallest change that would resolve it. Do not rewrite the whole artifact."
    )
    confidence: Confidence = Field(description="Your confidence that this issue is real.")
    unit_id: str | None = Field(
        default=None, description="The review unit id this issue belongs to, or null for global issues."
    )
    source_reviewer: str | None = Field(
        default=None, description="CODE-ADDED. The role key of the reviewer that raised it."
    )


class ReviewerReport(_Strict):
    """A read-only specialist's complete output."""

    reviewer: NonEmptyStr = Field(description="Your role key exactly as given in your instructions.")
    scope: NonEmptyStr = Field(description="What you inspected, with unit ids and boundaries.")
    issues: list[Issue] = Field(
        default_factory=list, description="Every concrete issue found. Empty list if the material is clean."
    )
    unresolved_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Questions you could not answer from the material. Uncertainty belongs here, not in "
            "the issues list."
        ),
    )
    overall_assessment: NonEmptyStr = Field(
        description="2-5 sentences. State what is sound and what is not, in that order, without hedging."
    )
    coverage_notes: list[str] = Field(
        default_factory=list,
        description="Anything you could not inspect and why (e.g. a unit was empty or unreadable).",
    )
    validation_notes: list[str] = Field(
        default_factory=list,
        description="CODE-ADDED. Application validators record repairs/downgrades here.",
    )

    def issues_of_severity(self, severities: frozenset[Severity]) -> list[Issue]:
        return [i for i in self.issues if i.severity in severities]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


class ReviewUnit(_Strict):
    """One bounded chunk of the target that reviewers can be assigned to."""

    unit_id: NonEmptyStr = Field(description="Short id, e.g. 'u1'. Must be unique in the plan.")
    location: NonEmptyStr = Field(
        description="Where it is: a heading, a line range such as 'L10-L40', a section name, or a file."
    )
    description: NonEmptyStr = Field(description="What this unit contains, in one sentence.")
    priority: Severity = Field(
        description="How much reviewer attention this unit deserves: critical > major > minor > info."
    )


class ReviewPlan(_Strict):
    """The decomposition produced before any reviewer runs."""

    units: list[ReviewUnit] = Field(description="The complete decomposition of the target. At least one unit.")
    selected_specialists: list[NonEmptyStr] = Field(
        description=(
            "Role keys of the specialists to run. Choose only from the available registry listed in "
            "your instructions; select at least two independent reviewers."
        )
    )
    objective_restatement: NonEmptyStr = Field(
        description="The user's objective restated precisely, including what 'done' means."
    )
    focus: list[str] = Field(
        default_factory=list, description="What reviewers should concentrate on, given the objective."
    )
    constraints: list[str] = Field(
        default_factory=list, description="Immutable constraints the workflow must respect."
    )
    rationale: NonEmptyStr = Field(description="Why this decomposition and this specialist set.")


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


class IssueResolution(_Strict):
    """Synthesis' decision about one issue id."""

    issue_id: NonEmptyStr = Field(description="The id of an issue you were given. Never invent an id.")
    disposition: Disposition = Field(
        description="accepted = real and actionable; rejected = not substantiated; merged = folded into another issue."
    )
    rationale: NonEmptyStr = Field(
        description="Why. For rejections, name the specific defect in the reviewer's reasoning."
    )
    action_required: bool = Field(
        description="True when an edit is needed to resolve it. Must be True for accepted critical/major issues."
    )
    assigned_to: NonEmptyStr = Field(
        description="One of the role keys from your instructions, or 'orchestrator'."
    )


class MergedIssue(_Strict):
    """A duplicate group. The canonical id must be first and must also be resolved."""

    issue_ids: list[NonEmptyStr] = Field(
        description="Two or more issue ids that are the same defect. Put the canonical id first."
    )
    rationale: NonEmptyStr = Field(description="Why these are the same defect.")


class IssueConflict(_Strict):
    """A place where reviewers materially disagree and the disagreement is unresolved."""

    issue_ids: list[NonEmptyStr] = Field(description="The conflicting issue ids.")
    summary: NonEmptyStr = Field(description="The disagreement in one sentence.")
    reason: NonEmptyStr = Field(description="Why it cannot be settled from the available evidence.")
    adjudicated: bool = Field(description="True if you decided which side is correct and why below.")
    resolution_note: str | None = Field(default=None, description="Your decision, when adjudicated is true.")


class ConsolidatedIssueMap(_Strict):
    """The single authoritative list of issues handed to the controlled editor."""

    accepted_issues: list[IssueResolution] = Field(
        default_factory=list, description="Issues accepted as real and worth acting on."
    )
    rejected_issues: list[IssueResolution] = Field(
        default_factory=list, description="Issues rejected as unsubstantiated or wrong."
    )
    merged_issues: list[MergedIssue] = Field(
        default_factory=list, description="Duplicate groups."
    )
    conflicts: list[IssueConflict] = Field(
        default_factory=list, description="Unresolved reviewer disagreements. Do not hide these."
    )
    unresolved_questions: list[str] = Field(
        default_factory=list, description="Open questions that block a decision."
    )
    change_required: bool = Field(
        description=(
            "True when at least one accepted issue needs an edit. Must be True if any accepted issue "
            "is critical or major."
        )
    )
    change_rationale: NonEmptyStr = Field(description="Why an edit is or is not required.")

    def accepted_ids(self) -> set[str]:
        return {r.issue_id for r in self.accepted_issues if r.action_required}

    def all_resolved_ids(self) -> set[str]:
        return {r.issue_id for r in [*self.accepted_issues, *self.rejected_issues]}


# --------------------------------------------------------------------------- #
# Controlled edit
# --------------------------------------------------------------------------- #


class EditChange(_Strict):
    """One applied change, as reported by the editor (CODE-VERIFIED against the real diff)."""

    change_id: NonEmptyStr = Field(description="Short id, e.g. 'c1'.")
    issue_ids: list[NonEmptyStr] = Field(
        default_factory=list, description="The accepted issue ids this change resolves. Use only ids you were given."
    )
    kind: ChangeKind = Field(description="insert, delete or replace.")
    location: NonEmptyStr = Field(description="Heading or line range touched.")
    before: str = Field(default="", description="The exact text removed, quoted verbatim. Empty for insert.")
    after: str = Field(default="", description="The exact text inserted, verbatim. Empty for delete.")
    rationale: NonEmptyStr = Field(description="Why this change is the smallest defensible fix.")


class EditResult(_Strict):
    """The controlled editor's report. The workflow recomputes the diff independently."""

    target: NonEmptyStr = Field(description="The target path you were granted. Do not touch other files.")
    changes: list[EditChange] = Field(
        default_factory=list, description="Every change you applied. Must be non-empty if you wrote anything."
    )
    unresolved_items: list[str] = Field(
        default_factory=list, description="Accepted issues you deliberately did not fix, and why not."
    )
    verification_requested: list[str] = Field(
        default_factory=list, description="Checks that should be run on your edit."
    )
    no_change_reason: str | None = Field(
        default=None, description="Required only when you applied no change: why no edit was defensible."
    )


# --------------------------------------------------------------------------- #
# Diff review / adversarial review
# --------------------------------------------------------------------------- #


class DiffReviewResult(_Strict):
    """Result of inspecting the diff (an LLM judgement layered over code-computed diff facts)."""

    reviewer: NonEmptyStr = Field(description="Your role key exactly as given.")
    out_of_scope_changes: list[str] = Field(
        default_factory=list, description="Changes unrelated to the accepted issues, quoted."
    )
    unsupported_assertions: list[str] = Field(
        default_factory=list,
        description="New factual/quantitative/causal claims introduced without evidence in the material.",
    )
    removed_content_risks: list[str] = Field(
        default_factory=list, description="Content removed that carried meaning or uncertainty."
    )
    semantic_drift: list[str] = Field(
        default_factory=list, description="Places where meaning changed beyond what the issue asked for."
    )
    notes: list[str] = Field(default_factory=list, description="Anything else the orchestrator must know.")
    approved: bool = Field(
        description="False if you found any out-of-scope change, unsupported assertion or semantic drift."
    )


class AdversarialFinding(_Strict):
    """One concrete reason to reject the revision."""

    description: NonEmptyStr = Field(description="The reason for rejection, stated as a defect.")
    location: NonEmptyStr = Field(description="Where in the revised artifact or diff this is visible.")
    evidence: NonEmptyStr = Field(description="Quote or line reference proving it.")
    related_issue_id: str | None = Field(
        default=None, description="The accepted issue id this finding relates to, if any."
    )


class AdversarialVerdict(_Strict):
    """The adversarial reviewer's verdict. Default posture: reject unless proven otherwise."""

    passed: bool = Field(
        description=(
            "True only if you actively failed to find a rejection reason after trying. A revision "
            "with any critical or major finding must be marked False."
        )
    )
    critical_issues: list[AdversarialFinding] = Field(
        default_factory=list, description="Fabrication, unsupported conclusions, misleading certainty, silent scope inflation."
    )
    major_issues: list[AdversarialFinding] = Field(
        default_factory=list, description="Incomplete fixes, unresolved contradictions, missing caveats, cherry-picking."
    )
    minor_issues: list[AdversarialFinding] = Field(
        default_factory=list, description="Small residual problems that do not block acceptance."
    )
    rationale: NonEmptyStr = Field(description="Why the revision should be accepted or rejected.")
    rejection_reasons: list[str] = Field(
        default_factory=list, description="One line per blocking reason. Empty when passed is true."
    )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


class VerificationResult(_Strict):
    """One executed verification check."""

    check: NonEmptyStr = Field(description="Name of the check.")
    command_or_method: NonEmptyStr = Field(description="The command run or the method applied.")
    passed: bool = Field(description="True only if the check ran and its assertions held.")
    evidence: str = Field(default="", description="Observed output evidence supporting the verdict.")
    error: str | None = Field(default=None, description="The error, when the check failed or could not run.")
    kind: VerificationKind = Field(description="CODE-SET. deterministic or semantic.")
    mandatory: bool = Field(description="CODE-SET. True when failure must block completion.")
    duration_ms: int = Field(default=0, description="CODE-SET. Wall-clock cost of the check.")


class VerificationReport(BaseModel):
    """Aggregate of every check executed in a VERIFY stage."""

    model_config = ConfigDict(extra="forbid")

    results: list[VerificationResult] = Field(default_factory=list)
    stage: Stage = Field(default=Stage.VERIFY)

    @property
    def mandatory_failures(self) -> list[VerificationResult]:
        return [r for r in self.results if r.mandatory and not r.passed]

    @property
    def all_mandatory_passed(self) -> bool:
        return not self.mandatory_failures and any(r.mandatory for r in self.results)


# --------------------------------------------------------------------------- #
# Semantic verification and deep-dive adjudication
# --------------------------------------------------------------------------- #


class SemanticVerification(_Strict):
    """One model-judged verification check."""

    check: NonEmptyStr = Field(description="The check name, copied exactly from the request.")
    passed: bool = Field(description="True only when you can quote evidence that the property holds.")
    method: NonEmptyStr = Field(description="How you checked: files read, search terms, reasoning applied.")
    evidence: NonEmptyStr = Field(
        description="Quoted text that proves or disproves the property, or an explicit statement that it is absent."
    )
    error: str | None = Field(default=None, description="Set when the check could not be performed.")


class VerificationBatch(_Strict):
    """All requested semantic checks, answered in a single call."""

    results: list[SemanticVerification] = Field(
        description="One entry per requested check. Report exactly the checks you were given, no others."
    )


class Adjudication(_Strict):
    """The deep-dive adjudicator's answer to one specific question."""

    question: NonEmptyStr = Field(description="The question, restated precisely.")
    verdict: NonEmptyStr = Field(
        description="Which proposition the evidence supports: the first, the second, both, neither, or insufficient evidence."
    )
    evidence: NonEmptyStr = Field(description="The decisive quoted text, or the reason none exists.")
    recommended_action: NonEmptyStr = Field(
        description="What synthesis or the editor should do with this answer, in one sentence."
    )


# --------------------------------------------------------------------------- #
# Guardrails / planning support
# --------------------------------------------------------------------------- #


class TaskScopeVerdict(BaseModel):
    """Output of the task-scope input guardrail."""

    model_config = ConfigDict(extra="forbid")

    in_scope: bool = Field(description="True when the request is a review/synthesis/edit task over the given target.")
    reason: str = Field(description="One sentence justification.")
    prohibited_request: bool = Field(
        default=False,
        description="True when the request asks to fabricate results, bypass verification, or disable review.",
    )


class FinalReport(_Strict):
    """The orchestrator's user-facing report.

    ``code_status`` is authoritative. ``claimed_status`` is what the model
    reported, and the output guardrail compares the two.
    """

    executive_summary: NonEmptyStr = Field(
        description="What was reviewed, what was found, what was changed, and what remains open."
    )
    accepted_issues_summary: list[str] = Field(
        default_factory=list, description="One line per accepted issue: id, severity, resolution."
    )
    rejected_issues_summary: list[str] = Field(
        default_factory=list, description="One line per rejected issue and why it was rejected."
    )
    reviewer_disagreements: list[str] = Field(
        default_factory=list, description="Conflicts between reviewers, kept visible rather than smoothed over."
    )
    changes_applied: list[str] = Field(
        default_factory=list, description="What the editor actually changed, as verified by the diff."
    )
    unresolved_issues: list[str] = Field(
        default_factory=list, description="Everything still open, with the reason it is not resolved."
    )
    verification_summary: list[str] = Field(
        default_factory=list, description="Each verification check with pass/fail and its evidence."
    )
    disclosures: list[str] = Field(
        default_factory=list,
        description="Honest limitations: failed reviewers, missing coverage, degraded confidence.",
    )
    claimed_status: WorkflowStatus = Field(
        description=(
            "The status you believe the workflow earned. You may not claim 'succeeded' if any mandatory "
            "verification failed, any reviewer failed, or any critical/major issue is unresolved."
        )
    )


# --------------------------------------------------------------------------- #
# Workflow-level results
# --------------------------------------------------------------------------- #


class UnresolvedIssue(BaseModel):
    """An issue still open at completion."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    summary: str
    severity: Severity
    source: str
    reason: str


class ReviewCoverage(BaseModel):
    """Which expected reviewers actually produced a valid report."""

    model_config = ConfigDict(extra="forbid")

    expected: list[str] = Field(default_factory=list)
    succeeded: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failed


class RoleUsage(BaseModel):
    """Token usage attributed to one role."""

    model_config = ConfigDict(extra="forbid")

    role: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0

    def merge(self, other: RoleUsage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        self.latency_ms += other.latency_ms


class UsageSummary(BaseModel):
    """Aggregated cost metadata for a workflow."""

    model_config = ConfigDict(extra="forbid")

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    wall_clock_ms: int = 0
    by_role: dict[str, RoleUsage] = Field(default_factory=dict)

    def add_call(self, role: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
        self.requests += 1
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens
        bucket = self.by_role.setdefault(role, RoleUsage(role=role))
        bucket.calls += 1
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        bucket.total_tokens += input_tokens + output_tokens
        bucket.latency_ms += latency_ms


class WorkflowResult(BaseModel):
    """The terminal result of one workflow run."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    status: WorkflowStatus
    status_reason: str
    stages_completed: list[Stage] = Field(default_factory=list)
    rounds_executed: int = 0
    unresolved_issues: list[UnresolvedIssue] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    final_output: FinalReport | None = None
    review_coverage: ReviewCoverage = Field(default_factory=ReviewCoverage)
    usage: UsageSummary = Field(default_factory=UsageSummary)
    failures: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only for a clean success. ``degraded`` is deliberately not ok."""
        return self.status is WorkflowStatus.SUCCEEDED
