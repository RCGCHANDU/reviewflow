"""Enumerations and severity ordering used by every module.

Using ``str`` mixins keeps every value JSON-serialisable, which matters because
these enums travel through structured-output schemas, the audit ledger and the
final report.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """How much an issue matters. Blocking severities gate the workflow."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.INFO: 3,
}

#: Severities that must be resolved before a workflow may report success.
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({Severity.CRITICAL, Severity.MAJOR})


def is_blocking(severity: Severity) -> bool:
    """True when the severity is high enough to gate completion."""
    return severity in BLOCKING_SEVERITIES


def severity_rank(severity: Severity) -> int:
    """Lower is more severe; used both for sorting and for max-of comparisons."""
    return _SEVERITY_ORDER[severity]


def most_severe(severities: list[Severity]) -> Severity | None:
    """Return the most severe entry, or ``None`` for an empty list."""
    if not severities:
        return None
    return min(severities, key=severity_rank)


class EvidenceStatus(str, Enum):
    """What kind of support backs an issue.

    This is the enum that keeps "observed evidence" from being confused with
    "inference" or "uncertainty" as it moves between agents.
    """

    OBSERVED = "observed"  # quoted or directly readable in the material
    INFERRED = "inferred"  # logically derived from observed material
    INTERPRETATION = "interpretation"  # a reading, not a fact
    UNCERTAIN = "uncertain"  # reviewer could not decide
    CONTRADICTED = "contradicted"  # conflicts with other observed material
    ABSENT = "absent"  # no support found at all


#: Evidence statuses strong enough to justify a blocking (critical/major) issue.
GROUNDING_EVIDENCE_STATUSES: frozenset[EvidenceStatus] = frozenset(
    {EvidenceStatus.OBSERVED, EvidenceStatus.INFERRED, EvidenceStatus.CONTRADICTED}
)


class IssueCategory(str, Enum):
    """Issue taxonomy. Categories are deliberately coarse so reviewers agree."""

    # evidence / integrity auditor
    UNSUPPORTED_CLAIM = "unsupported_claim"
    EVIDENCE_PROVENANCE = "evidence_provenance"
    QUANTITATIVE_ERROR = "quantitative_error"
    SCOPE_MISMATCH = "scope_mismatch"
    CAUSAL_OVERREACH = "causal_overreach"
    MISSING_UNCERTAINTY = "missing_uncertainty"
    INCOMPATIBLE_COMPARISON = "incompatible_comparison"
    FACTUAL_INCONSISTENCY = "factual_inconsistency"
    # rhetorical analyst
    PURPOSE = "purpose"
    CLARITY = "clarity"
    TERMINOLOGY = "terminology"
    INFORMATION_FLOW = "information_flow"
    REDUNDANCY = "redundancy"
    LOCAL_COHERENCE = "local_coherence"
    STANCE = "stance"
    AMBIGUITY = "ambiguity"
    # technical reviewer
    METHODOLOGY = "methodology"
    REPRODUCIBILITY = "reproducibility"
    HIDDEN_ASSUMPTION = "hidden_assumption"
    EVIDENCE_INTERPRETATION = "evidence_interpretation"
    TECHNICAL_LIMITATION = "technical_limitation"
    CONCLUSION_WARRANT = "conclusion_warrant"
    # structure reviewer
    LOGICAL_PROGRESSION = "logical_progression"
    MISSING_COMPONENT = "missing_component"
    CROSS_SECTION_CONTRADICTION = "cross_section_contradiction"
    PROMISE_VS_DELIVERY = "promise_vs_delivery"
    TERMINOLOGY_CONSISTENCY = "terminology_consistency"
    GLOBAL_COHERENCE = "global_coherence"
    # adversarial reviewer only
    FABRICATION = "fabrication"
    SEMANTIC_DRIFT = "semantic_drift"
    INCOMPLETE_FIX = "incomplete_fix"
    MISLEADING_CERTAINTY = "misleading_certainty"
    VERIFICATION_GAP = "verification_gap"
    OTHER = "other"


class Confidence(str, Enum):
    """Reviewer self-reported confidence in an issue."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Disposition(str, Enum):
    """What synthesis decided to do with one issue."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MERGED = "merged"


class Stage(str, Enum):
    """Workflow states. Transitions between them are validated by code."""

    INGEST = "ingest"
    PLAN = "plan"
    PARALLEL_REVIEW = "parallel_review"
    SYNTHESIZE = "synthesize"
    CONTROLLED_EDIT = "controlled_edit"
    DIFF_REVIEW = "diff_review"
    ADVERSARIAL_REVIEW = "adversarial_review"
    VERIFY = "verify"
    REVISE = "revise"
    FINAL_REVIEW = "final_review"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkflowStatus(str, Enum):
    """Terminal workflow status.

    ``DEGRADED`` exists so that a workflow which lost a reviewer's coverage can
    never be reported as success.
    """

    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    FAILED = "failed"


class StageOutcome(str, Enum):
    """Recorded outcome of one stage attempt in the audit ledger."""

    OK = "ok"
    #: The stage produced usable output but not everything succeeded (e.g. some reviewers
    #: failed while others reported). Distinct from OK so the ledger does not claim a clean
    #: stage, and distinct from FAILED so a degraded-but-usable stage is not overstated.
    DEGRADED = "degraded"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    TOOL_ERROR = "tool_error"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    FAILED = "failed"
    RETRIED = "retried"


class FailureKind(str, Enum):
    """Error classification. Retry policy is keyed on this, never on the message."""

    TRANSIENT = "transient"  # network/rate-limit/5xx: retry is safe
    INVALID_OUTPUT = "invalid_output"  # schema/logic violation: retry once with feedback
    TIMEOUT = "timeout"  # wall-clock budget exhausted
    LOGICAL = "logical"  # the agent legitimately failed at its task: do not retry
    GUARDRAIL = "guardrail"  # a guardrail (SDK or app) blocked the step
    INFRASTRUCTURE = "infrastructure"  # missing key, bad path, unreadable file


class VerificationKind(str, Enum):
    """Whether a verification check is deterministic or model-based."""

    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"


class ChangeKind(str, Enum):
    """Kind of edit applied by the controlled editor."""

    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"


class AgentRole(str, Enum):
    """Canonical role keys.

    ``AgentRole`` enumerates the shipped specialists. The registry accepts any
    string key, so a new specialist does not require an engine change (and hence
    new roles may live outside this enum).
    """

    ORCHESTRATOR = "orchestrator"
    EVIDENCE_AUDITOR = "evidence_auditor"
    RHETORICAL_ANALYST = "rhetorical_analyst"
    TECHNICAL_REVIEWER = "technical_reviewer"
    STRUCTURE_REVIEWER = "structure_reviewer"
    ADVERSARIAL_REVIEWER = "adversarial_reviewer"
    CONTROLLED_EDITOR = "controlled_editor"
    VERIFIER = "verifier"
    DEEP_DIVE_ADJUDICATOR = "deep_dive_adjudicator"


class IssueBucket(str, Enum):
    """Which bucket of the consolidated issue map an issue landed in."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MERGED = "merged"
