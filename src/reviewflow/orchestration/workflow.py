"""The workflow engine.

Deterministic Python drives the stages; LLM judgement happens strictly inside a
bounded stage. The engine owns:

* the state machine (:mod:`reviewflow.orchestration.machine`);
* the ingest snapshot and the workspace snapshots that prove nothing else changed;
* bounded parallel review with one independent packet per reviewer;
* synthesis, which is the only place reviewer outputs meet;
* the edit grant and the single-writer lock around the controlled edit;
* the code-computed diff, and therefore the *facts* about what changed;
* the completion gates: no success without passed verification, an accepted
  adversarial verdict and full reviewer coverage.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from agents import Agent, ModelProvider

from reviewflow.agents.factory import AgentFactory
from reviewflow.agents.registry import SpecialistRegistry, build_default_registry
from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.errors import ReviewflowError
from reviewflow.models.contracts import (
    AdversarialVerdict,
    ConsolidatedIssueMap,
    DiffReviewResult,
    EditResult,
    FinalReport,
    Issue,
    ReviewCoverage,
    ReviewerReport,
    ReviewPlan,
    ReviewUnit,
    UnresolvedIssue,
    VerificationReport,
    WorkflowResult,
)
from reviewflow.models.enums import (
    FailureKind,
    Severity,
    Stage,
    StageOutcome,
    WorkflowStatus,
    is_blocking,
    severity_rank,
)
from reviewflow.observability.logging_setup import get_logger
from reviewflow.orchestration import packets
from reviewflow.orchestration.ingest import Ingester, IngestResult
from reviewflow.orchestration.invoker import (
    AgentInvoker,
    InvocationRequest,
    InvocationResult,
    RunConfigFactory,
    SdkAgentInvoker,
)
from reviewflow.orchestration.limits import RetryPolicy
from reviewflow.orchestration.machine import StateMachine
from reviewflow.persistence.ledger import Ledger, open_ledger
from reviewflow.persistence.records import AuditEvent, EventType, WorkflowRunRecord
from reviewflow.providers.routing import ModelRouter
from reviewflow.validation import (
    ValidationOutcome,
    check_reviewer_report,
    normalize_reviewer_report,
    validate_adversarial_verdict,
    validate_edit_result,
    validate_issue_map,
    validate_plan,
)
from reviewflow.verification import (
    DEFAULT_SEMANTIC_CHECKS,
    SEMANTIC_CHECKS,
    CheckContext,
    DiffEvidence,
    VerificationRunner,
    compute_diff,
    empty_diff,
    snapshot_workspace,
)
from reviewflow.verification.diff import sha256_bytes, with_disk_hashes

_log = get_logger("workflow")

#: Stage order used for the human-readable trace summary.
_STAGE_LABEL = {
    Stage.INGEST: "ingest",
    Stage.PLAN: "plan",
    Stage.PARALLEL_REVIEW: "parallel_review",
    Stage.SYNTHESIZE: "synthesize",
    Stage.CONTROLLED_EDIT: "controlled_edit",
    Stage.DIFF_REVIEW: "diff_review",
    Stage.ADVERSARIAL_REVIEW: "adversarial_review",
    Stage.VERIFY: "verify",
    Stage.REVISE: "revise",
    Stage.FINAL_REVIEW: "final_review",
    Stage.COMPLETE: "complete",
    Stage.BLOCKED: "blocked",
    Stage.FAILED: "failed",
}


@dataclass
class GateDecision:
    """Outcome of the completion gates."""

    stage: Stage
    status: WorkflowStatus | None
    reason: str

    @property
    def terminal(self) -> bool:
        return self.status is not None


@dataclass
class CoverageTracker:
    """Tracks which reviewers were expected, which reported, and which failed.

    A reviewer that was expected but never ran is reported as *failed*, never as
    succeeded: coverage is a claim about what actually happened.
    """

    expected: set[str] = field(default_factory=set)
    succeeded: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)
    #: Non-reviewer degradations (for example truncated material) that must be disclosed.
    notes: list[str] = field(default_factory=list)

    def note_failure(self, role_key: str, reason: str) -> None:
        self.failed[role_key] = reason

    def resolve(self) -> ReviewCoverage:
        missing = dict.fromkeys(sorted(self.expected - self.succeeded - set(self.failed)), "did not run")
        failed = {**missing, **self.failed}
        return ReviewCoverage(
            expected=sorted(self.expected | set(failed)),
            succeeded=sorted(self.succeeded),
            failed=sorted(failed),
        )

    @property
    def degraded(self) -> bool:
        return bool(self.failed) or bool(self.notes)


def assign_units(plan: ReviewPlan, reviewer_keys: Sequence[str]) -> dict[str, list[str]]:
    """Deal units to reviewers, most severe first, so every unit has an owner.

    Round-robin over severity-sorted units gives the critical units the widest
    coverage while keeping every unit reviewed at least once. Every reviewer also
    receives the whole material; the assignment only tells it where to spend
    attention, which is why global reviewers still function.
    """
    ordered = sorted(plan.units, key=lambda u: (severity_rank(u.priority), u.unit_id))
    assignment: dict[str, list[str]] = {key: [] for key in reviewer_keys}
    if not reviewer_keys:
        return assignment
    for index, unit in enumerate(ordered):
        assignment[reviewer_keys[index % len(reviewer_keys)]].append(unit.unit_id)
    return assignment


def evaluate_completion(
    *,
    round_index: int,
    max_rounds: int,
    verification: VerificationReport | None,
    verdict: AdversarialVerdict | None,
    unresolved_blocking: list[UnresolvedIssue],
    coverage_degraded: bool,
    critical_violation: str | None,
) -> GateDecision:
    """Decide whether the workflow may complete, must revise, or is blocked.

    Order matters: a critical violation cannot be revised away, a failed mandatory
    verification always blocks, and a rejected adversarial verdict always blocks.
    Only once every gate passes can the workflow consider degraded coverage.
    """
    if critical_violation is not None:
        return GateDecision(Stage.BLOCKED, WorkflowStatus.BLOCKED, f"critical violation: {critical_violation}")

    if verification is None or not verification.results:
        return GateDecision(Stage.BLOCKED, WorkflowStatus.BLOCKED, "no verification was executed")
    failed_checks = verification.mandatory_failures
    if failed_checks:
        names = ", ".join(result.check for result in failed_checks)
        if round_index < max_rounds + 1:
            return GateDecision(Stage.REVISE, None, f"mandatory verification failed: {names}")
        return GateDecision(Stage.BLOCKED, WorkflowStatus.BLOCKED, f"mandatory verification still failing: {names}")

    if verdict is not None and not verdict.passed:
        blockers = len(verdict.critical_issues) + len(verdict.major_issues)
        if round_index < max_rounds + 1:
            return GateDecision(Stage.REVISE, None, f"adversarial verdict rejected the revision ({blockers} blocking findings)")
        return GateDecision(Stage.BLOCKED, WorkflowStatus.BLOCKED, "adversarial reviewer rejected every attempt")

    if unresolved_blocking:
        ids = ", ".join(item.issue_id for item in unresolved_blocking)
        if round_index < max_rounds + 1:
            return GateDecision(Stage.REVISE, None, f"blocking issues remain unresolved: {ids}")
        return GateDecision(
            Stage.BLOCKED, WorkflowStatus.BLOCKED, f"blocking issues unresolved after {round_index} round(s): {ids}"
        )

    if coverage_degraded:
        return GateDecision(Stage.FINAL_REVIEW, WorkflowStatus.DEGRADED, "gates passed but reviewer coverage is incomplete")
    return GateDecision(Stage.FINAL_REVIEW, WorkflowStatus.SUCCEEDED, "all gates passed with full coverage")


class ReviewWorkflow:
    """Runs one review workflow end to end."""

    def __init__(
        self,
        settings: Settings,
        *,
        ledger: Ledger | None = None,
        registry: SpecialistRegistry | None = None,
        invoker: AgentInvoker | None = None,
        factory: AgentFactory | None = None,
        verification_runner: VerificationRunner | None = None,
        ingester: Ingester | None = None,
        model_provider: ModelProvider | None = None,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings.resolved() if settings.workspace_root else settings
        #: Provider routing, and the reason the framework is "plug and play": when
        #: nothing is injected at all - the real-run path - the workflow discovers
        #: whatever credentials this machine already has (an OpenAI ChatGPT OAuth
        #: login, a commandcode login, or a plain API key) and routes every role
        #: through them. An explicitly injected provider (the scripted test path)
        #: always wins and disables discovery, so tests never touch a network or a
        #: credential store.
        router: ModelRouter | None = None
        if factory is None and invoker is None:
            if isinstance(model_provider, ModelRouter):
                # An injected router *is* the router: it must also decide model names, not
                # just serve as the transport provider. Treating it as transport only left
                # AgentFactory with no router, so it fell back to ModelsByRole's
                # provider-agnostic placeholders and every role ran on a model named for a
                # different backend - which on a non-OpenAI provider is a hard failure.
                router = model_provider
            elif model_provider is None:
                router = ModelRouter.from_settings(self.settings)
                if router is not None:
                    model_provider = router
        self.router = router
        #: Optional observer of per-agent lifecycle events (start/completion/failure).
        #: ``None`` means the engine behaves exactly as it did before this parameter
        #: existed; the application passes a sink so its UI can show live progress.
        self.event_sink = event_sink
        #: Kept so nested runs (guardrail agents, agent-as-tool) stay on the same provider.
        self.model_provider = model_provider
        self.registry = registry or build_default_registry()
        self.ledger = ledger or open_ledger(self.settings.ledger_path)
        self.factory = factory or AgentFactory(self.settings, self.registry, router=router)
        self.run_config_factory = RunConfigFactory(self.settings, workflow_id="pending", model_provider=model_provider)
        self.invoker = invoker or SdkAgentInvoker(
            self.settings,
            run_config_factory=self.run_config_factory,
            retry_policy=RetryPolicy(self.settings.budget.retries),
            router=router,
            on_event=event_sink,
        )
        self.verification_runner = verification_runner or VerificationRunner(
            semantic_checks=tuple(
                self.settings.semantic_checks
                if self.settings.semantic_checks is not None
                else DEFAULT_SEMANTIC_CHECKS
            )
        )
        self.ingester = ingester or Ingester(self.settings)
        #: Exactly one writer: the edit stage holds this lock so no two mutations
        #: can ever be in flight, even if a future stage is added by mistake.
        self._write_lock = asyncio.Lock()

    # -- public API -------------------------------------------------------- #

    async def run(
        self,
        *,
        objective: str,
        target: Path | str,
        constraints: list[str] | None = None,
        user_preferences: dict[str, str] | None = None,
        verification_config: dict[str, object] | None = None,
    ) -> WorkflowResult:
        """Execute the workflow and return its terminal result."""
        started = time.monotonic()
        target_path = Path(target)
        ctx = WorkflowContext.create(
            objective=objective,
            target_path=target_path if target_path.is_absolute() else self.settings.workspace_root / target_path,
            workspace_root=self.settings.workspace_root,
            allowed_write_roots=self.settings.allowed_write_roots,
            constraints=constraints,
            generated_globs=self.settings.generated_globs,
            min_successful_reviewers=self.settings.min_successful_reviewers,
            user_preferences=user_preferences,
            verification_config=verification_config or {},
        )
        ctx.settings = self.settings
        ctx.model_provider = self.model_provider
        ctx.audit_hook = lambda event, detail: self._record(
            ctx,
            EventType.TOOL_CALL if event in {"tool_call", "mutation"} else EventType.GUARDRAIL,
            outcome=StageOutcome.OK,
            detail={"event": event, **detail},
        )
        self.run_config_factory.workflow_id = ctx.workflow_id
        machine = StateMachine(ctx.workflow_id, self.ledger)
        return await self._drive(ctx, machine, started)

    def _runner_for(self, verification_config: dict[str, object]) -> VerificationRunner:
        """Resolve the verification runner for a run, honouring a per-run override."""
        override = verification_config.get("semantic_checks")
        if override is None:
            return self.verification_runner
        if not isinstance(override, (list, tuple)):
            raise ReviewflowError(f"semantic_checks override must be a list, got {type(override).__name__}")
        names = tuple(str(name) for name in override)
        unknown = sorted(set(names) - set(SEMANTIC_CHECKS))
        if unknown:
            raise ReviewflowError(f"unknown semantic checks requested: {unknown}")
        return VerificationRunner(semantic_checks=names)

    # -- stage driver ------------------------------------------------------ #

    async def _drive(self, ctx: WorkflowContext, machine: StateMachine, started: float) -> WorkflowResult:
        failures: list[str] = []
        unresolved_findings: list[str] = []
        unresolved_issues: list[UnresolvedIssue] = []
        coverage = CoverageTracker()
        critical_violation: str | None = None
        rounds_executed = 0

        issue_map: ConsolidatedIssueMap | None = None
        corpus: list[Issue] = []
        verification: VerificationReport | None = None
        verdict: AdversarialVerdict | None = None
        diff_review: DiffReviewResult | None = None
        diff: DiffEvidence | None = None
        text_before = ""
        text_after = ""
        edit_result: EditResult | None = None
        reports: list[ReviewerReport] = []

        try:
            # ---------------- INGEST ---------------- #
            self._stage_start(ctx, machine, Stage.INGEST)
            snapshot_before = snapshot_workspace(
                ctx.workspace_root,
                source_globs=self.settings.source_globs,
                skip_generated=self.settings.generated_globs,
            )
            ingest = self.ingester.ingest(ctx)
            text_before = ingest.text
            text_after = ingest.text
            ctx.disclosures.extend(ingest.warnings)
            self._stage_end(ctx, Stage.INGEST, StageOutcome.OK, detail={"target": ingest.relative_path})

            # ---------------- PLAN ---------------- #
            machine.transition(Stage.PLAN, reason="ingest complete")
            ctx.stage = Stage.PLAN
            self._stage_start(ctx, machine, Stage.PLAN)
            plan_result = await self._invoke(
                ctx,
                self.factory.build_planner(),
                packets.plan_packet(ctx, ingest),
                role="orchestrator",
                stage=Stage.PLAN,
                validator=lambda plan: validate_plan(
                    plan, registry_keys=self.registry.keys(), write_capable_keys={s.role_key for s in self.registry.writers()}
                ),
            )
            if not plan_result.ok:
                return self._fail(ctx, machine, Stage.PLAN, plan_result, failures, started)
            plan = plan_result.output
            self._capture(ctx, "plan", plan)
            if not isinstance(plan, ReviewPlan):  # pragma: no cover - defensive
                machine.transition(Stage.FAILED, reason="planner returned an unexpected type")
                return self._finalize(
                    ctx, machine, WorkflowStatus.FAILED, "planner returned an unexpected type",
                    [], None, None, coverage, failures, 0, started,
                )
            self._stage_end(ctx, Stage.PLAN, StageOutcome.OK, detail={"units": len(plan.units)})

            # ---------------- PARALLEL_REVIEW ---------------- #
            machine.transition(Stage.PARALLEL_REVIEW, reason="plan accepted")
            ctx.stage = Stage.PARALLEL_REVIEW
            self._stage_start(ctx, machine, Stage.PARALLEL_REVIEW)
            rounds_executed = 1
            ctx.round_index = rounds_executed
            reviewer_keys = [key for key in plan.selected_specialists if not self.registry.get(key).may_mutate]
            assignment = assign_units(plan, reviewer_keys)
            coverage.expected |= set(reviewer_keys)
            reports, failed_reviewers = await self._parallel_review(ctx, plan, ingest, assignment)
            self._capture(ctx, "reviewer_reports", reports)
            self._capture(ctx, "failed_reviewers", failed_reviewers)
            corpus = [issue for report in reports for issue in report.issues]
            coverage.succeeded |= {report.reviewer for report in reports}
            for role_key, reason in failed_reviewers.items():
                coverage.note_failure(role_key, reason)
                ctx.note_failed_role(role_key, reason)
                failures.append(f"reviewer {role_key} failed: {reason}")
            if not reports:
                critical_violation = "no reviewer produced a usable report"
            self._stage_end(
                ctx,
                Stage.PARALLEL_REVIEW,
                StageOutcome.OK if reports else StageOutcome.FAILED,
                detail={"reports": len(reports), "issues": len(corpus), "failed": sorted(failed_reviewers)},
            )
            if not reports:
                machine.transition(Stage.FAILED, reason=critical_violation or "no reports")
                return self._finalize(ctx, machine, WorkflowStatus.FAILED, critical_violation or "", unresolved_issues, verification, None, coverage, failures, rounds_executed, started)

            # ---------------- revision loop ---------------- #
            while True:
                machine.transition(Stage.SYNTHESIZE, reason=f"round {rounds_executed}")
                ctx.stage = Stage.SYNTHESIZE
                self._stage_start(ctx, machine, Stage.SYNTHESIZE)
                synthesis_packet = packets.synthesis_packet(
                    ctx,
                    ingest=ingest,
                    reports=reports,
                    round_index=rounds_executed,
                    unresolved_findings=unresolved_findings,
                    prior_map=issue_map,
                )
                synth_result = await self._invoke(
                    ctx,
                    self.factory.build_synthesizer(),
                    synthesis_packet,
                    role="orchestrator",
                    stage=Stage.SYNTHESIZE,
                    # ``corpus`` is bound as a default so a validator always sees the revision
                    # it was built for, never a later one.
                    validator=self._issue_map_validator(corpus),
                )
                if not synth_result.ok:
                    if synth_result.failure_kind is FailureKind.LOGICAL:
                        machine.transition(Stage.FAILED, reason="synthesis could not produce a valid map")
                        return self._finalize(ctx, machine, WorkflowStatus.FAILED, "synthesis failed", unresolved_issues, verification, edit_result, coverage, failures, rounds_executed, started)
                    machine.transition(Stage.FAILED, reason="synthesis failed")
                    return self._finalize(ctx, machine, WorkflowStatus.FAILED, synth_result.error or "synthesis failed", unresolved_issues, verification, edit_result, coverage, failures, rounds_executed, started)
                issue_map = synth_result.output
                self._capture(ctx, "issue_map", issue_map)
                if isinstance(issue_map, ConsolidatedIssueMap):
                    self._notify(
                        "synthesis.completed",
                        accepted=[item.issue_id for item in issue_map.accepted_issues],
                        rejected=[item.issue_id for item in issue_map.rejected_issues],
                        merged=[list(merged.issue_ids) for merged in issue_map.merged_issues],
                        conflicts=[conflict.summary for conflict in issue_map.conflicts],
                        change_required=issue_map.change_required,
                        unresolved_questions=list(issue_map.unresolved_questions),
                    )
                if not isinstance(issue_map, ConsolidatedIssueMap):  # pragma: no cover - defensive
                    machine.transition(Stage.FAILED, reason="synthesis returned an unexpected type")
                    return self._finalize(
                        ctx, machine, WorkflowStatus.FAILED, "synthesis returned an unexpected type",
                        unresolved_issues, verification, edit_result, coverage, failures, rounds_executed, started,
                    )
                corpus = self._reconcile_corpus(issue_map, corpus, synth_result.validation_notes)
                self._stage_end(
                    ctx,
                    Stage.SYNTHESIZE,
                    StageOutcome.OK,
                    detail={
                        "accepted": len(issue_map.accepted_issues),
                        "rejected": len(issue_map.rejected_issues),
                        "conflicts": len(issue_map.conflicts),
                        "change_required": issue_map.change_required,
                    },
                )
                unresolved_issues = self._unresolved_from_map(issue_map, corpus, set())

                # ---------------- controlled edit ---------------- #
                if issue_map.change_required:
                    machine.transition(Stage.CONTROLLED_EDIT, reason="accepted issues require an action")
                    ctx.stage = Stage.CONTROLLED_EDIT
                    self._stage_start(ctx, machine, Stage.CONTROLLED_EDIT)
                    edit_result, diff, text_after = await self._controlled_edit(
                        ctx, ingest, issue_map, corpus, unresolved_findings, rounds_executed
                    )
                    ctx.revoke_grant()
                    self._capture(ctx, "edit_result", edit_result)
                    self._capture(ctx, "diff", diff)
                    if isinstance(edit_result, EditResult):
                        self._notify(
                            "edit.completed",
                            target=edit_result.target,
                            changed=bool(diff and diff.changed),
                            change_count=len(edit_result.changes),
                            unresolved_items=list(edit_result.unresolved_items),
                        )
                    self._stage_end(
                        ctx,
                        Stage.CONTROLLED_EDIT,
                        # The editor returning a usable report is OK; a failed edit is
                        # recorded as a failure event elsewhere and surfaces through verification.
                        StageOutcome.OK if edit_result is not None else StageOutcome.FAILED,
                        detail={
                            "changed": bool(diff and diff.changed),
                            "added": diff.added_lines if diff else 0,
                            "removed": diff.removed_lines if diff else 0,
                        },
                    )
                    if diff is None:  # pragma: no cover - defensive
                        machine.transition(Stage.FAILED, reason="no diff evidence produced")
                        return self._finalize(ctx, machine, WorkflowStatus.FAILED, "edit produced no diff evidence", unresolved_issues, verification, edit_result, coverage, failures, rounds_executed, started)

                    # ---------------- diff review ---------------- #
                    machine.transition(Stage.DIFF_REVIEW, reason="edit applied")
                    ctx.stage = Stage.DIFF_REVIEW
                    self._stage_start(ctx, machine, Stage.DIFF_REVIEW)
                    diff_result = await self._invoke(
                        ctx,
                        self.factory.build_diff_reviewer(),
                        packets.diff_review_packet(
                            ctx,
                            ingest=self._reingest(ctx, ingest, text_after),
                            diff_text=diff.unified_diff,
                            stats=diff.stats(),
                            issue_map=issue_map,
                            corpus=corpus,
                            round_index=rounds_executed,
                        ),
                        role="diff_reviewer",
                        stage=Stage.DIFF_REVIEW,
                    )
                    diff_review = (
                        diff_result.output if diff_result.ok and isinstance(diff_result.output, DiffReviewResult) else None
                    )
                    self._capture(ctx, "diff_review", diff_review)
                    if isinstance(diff_review, DiffReviewResult):
                        self._notify(
                            "diff_review.completed",
                            approved=diff_review.approved,
                            out_of_scope_changes=list(diff_review.out_of_scope_changes),
                            unsupported_assertions=list(diff_review.unsupported_assertions),
                            semantic_drift=list(diff_review.semantic_drift),
                        )
                    if diff_review is None:
                        failures.append(f"diff reviewer failed: {diff_result.error}")
                        ctx.note_failed_role("diff_reviewer", diff_result.error or "unknown")
                        unresolved_findings.append(
                            f"diff review unavailable ({diff_result.failure_kind.value if diff_result.failure_kind else 'error'}); the diff was not independently reviewed"
                        )
                    elif not diff_review.approved:
                        unresolved_findings.extend(
                            [f"diff review: {item}" for item in (*diff_review.out_of_scope_changes, *diff_review.unsupported_assertions, *diff_review.semantic_drift)]
                        )
                    self._stage_end(
                        ctx,
                        Stage.DIFF_REVIEW,
                        StageOutcome.OK if diff_result.ok else StageOutcome.FAILED,
                        detail={"approved": getattr(diff_review, "approved", None)},
                    )

                ingest_after = self._reingest(ctx, ingest, text_after)

                # ---------------- adversarial review ---------------- #
                machine.transition(Stage.ADVERSARIAL_REVIEW, reason="revision ready for falsification")
                ctx.stage = Stage.ADVERSARIAL_REVIEW
                self._stage_start(ctx, machine, Stage.ADVERSARIAL_REVIEW)
                adversarial_agent = self.factory.build_adversarial_reviewer()
                adversarial_input = packets.adversarial_packet(
                    ctx,
                    ingest=ingest_after,
                    diff_text=diff.unified_diff if diff else "(no change was applied)",
                    issue_map=issue_map,
                    corpus=corpus,
                    round_index=rounds_executed,
                    unresolved_findings=unresolved_findings,
                    no_change=not (diff and diff.changed),
                )
                verdict_result = await self._invoke(
                    ctx,
                    adversarial_agent,
                    adversarial_input,
                    role="adversarial_reviewer",
                    stage=Stage.ADVERSARIAL_REVIEW,
                    validator=self._verdict_validator(corpus),
                )
                verdict = (
                    verdict_result.output
                    if verdict_result.ok and isinstance(verdict_result.output, AdversarialVerdict)
                    else None
                )
                self._capture(ctx, "adversarial_verdict", verdict)
                if isinstance(verdict, AdversarialVerdict):
                    self._notify(
                        "adversarial.completed",
                        passed=verdict.passed,
                        critical=len(verdict.critical_issues),
                        major=len(verdict.major_issues),
                        minor=len(verdict.minor_issues),
                        rationale=verdict.rationale,
                        rejection_reasons=list(verdict.rejection_reasons),
                    )
                if verdict is not None:
                    unresolved_findings.extend(self._verdict_findings(verdict))
                else:
                    failures.append(f"adversarial reviewer failed: {verdict_result.error}")
                    ctx.note_failed_role("adversarial_reviewer", verdict_result.error or "unknown")
                    unresolved_findings.append("adversarial review did not complete; the revision is not falsification-tested")
                self._stage_end(
                    ctx,
                    Stage.ADVERSARIAL_REVIEW,
                    StageOutcome.OK if verdict_result.ok else StageOutcome.FAILED,
                    detail={"passed": verdict.passed if verdict else None},
                )

                # ---------------- verification ---------------- #
                machine.transition(Stage.VERIFY, reason="all review stages complete")
                ctx.stage = Stage.VERIFY
                self._stage_start(ctx, machine, Stage.VERIFY)
                target_bytes = ctx.target_path.read_bytes()
                target_digest = sha256_bytes(target_bytes)
                base_diff = diff or with_disk_hashes(
                    empty_diff(ingest.relative_path, text_after),
                    sha_before=target_digest,
                    sha_after=target_digest,
                )
                snapshot_after = snapshot_workspace(
                    ctx.workspace_root,
                    source_globs=self.settings.source_globs,
                    skip_generated=self.settings.generated_globs,
                )
                out_of_scope = sorted(
                    set(snapshot_before.changed_paths(snapshot_after))
                    - {ctx.target_path.relative_to(ctx.workspace_root).as_posix()}
                )
                if out_of_scope:
                    critical_violation = f"files changed outside the edit grant: {out_of_scope}"
                    failures.append(critical_violation)
                if ingest.truncated:
                    coverage.notes.append("material was truncated at ingest; reviewers could not see all of it")

                check_context = CheckContext(
                    context=ctx,
                    settings=self.settings,
                    ingest=ingest_after,
                    diff=base_diff,
                    issue_map=issue_map,
                    corpus=corpus,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    text_before=text_before,
                    text_after=text_after,
                )
                runner = self._runner_for(ctx.verification_config)
                results = runner.run_deterministic(check_context)
                if diff and diff.changed and runner.semantic_checks:
                    results.extend(await self._semantic_verification(ctx, ingest_after, issue_map, corpus, diff, runner))
                verification = VerificationRunner.build_report(results)
                self._capture(ctx, "verification", verification)
                failing = [result.check for result in verification.results if not result.passed]
                self._notify(
                    "verification.completed",
                    total=len(verification.results),
                    failed=len(failing),
                    failing_checks=failing,
                )
                verification_outcome = self._record_verification(ctx, verification)
                if not verification_outcome.ok:
                    failures.extend(verification_outcome.problems)
                    unresolved_findings.extend(verification_outcome.problems)
                self._stage_end(
                    ctx,
                    Stage.VERIFY,
                    StageOutcome.OK if verification.all_mandatory_passed else StageOutcome.FAILED,
                    detail={
                        "passed": [r.check for r in verification.results if r.passed],
                        "failed": [r.check for r in verification.results if not r.passed],
                    },
                )

                resolved_ids = self._resolved_issue_ids(issue_map, edit_result, diff)
                unresolved_issues = self._unresolved_from_map(
                    issue_map, corpus, resolved_ids
                ) + self._unresolved_from_gates(verdict, verification, unresolved_findings)
                blocking = [item for item in unresolved_issues if is_blocking(item.severity)]
                decision = evaluate_completion(
                    round_index=rounds_executed,
                    max_rounds=self.settings.budget.max_revision_rounds,
                    verification=verification,
                    verdict=verdict,
                    unresolved_blocking=blocking,
                    coverage_degraded=coverage.degraded or ingest.truncated,
                    critical_violation=critical_violation,
                )
                self._record(
                    ctx,
                    EventType.STAGE_END,
                    stage=Stage.VERIFY,
                    outcome=StageOutcome.OK if decision.terminal else StageOutcome.RETRIED,
                    detail={"decision": decision.stage.value, "status": decision.status.value if decision.status else None, "reason": decision.reason},
                )

                if decision.stage is Stage.REVISE:
                    machine.transition(Stage.REVISE, reason=decision.reason)
                    ctx.stage = Stage.REVISE
                    self._stage_start(ctx, machine, Stage.REVISE)
                    rounds_executed += 1
                    ctx.round_index = rounds_executed
                    self._stage_end(ctx, Stage.REVISE, StageOutcome.OK, detail={"next_round": rounds_executed, "reason": decision.reason})
                    continue

                if decision.stage is Stage.FINAL_REVIEW:
                    machine.transition(Stage.FINAL_REVIEW, reason=decision.reason)
                    return await self._final_review(
                        ctx,
                        machine,
                        status=decision.status or WorkflowStatus.DEGRADED,
                        reason=decision.reason,
                        ingest=ingest_after,
                        issue_map=issue_map,
                        corpus=corpus,
                        verification=verification,
                        verdict=verdict,
                        diff_review=diff_review,
                        diff=base_diff,
                        unresolved_issues=unresolved_issues,
                        coverage=coverage,
                        failures=failures,
                        rounds_executed=rounds_executed,
                        started=started,
                    )

                machine.transition(Stage.BLOCKED, reason=decision.reason)
                return self._finalize(
                    ctx,
                    machine,
                    decision.status or WorkflowStatus.BLOCKED,
                    decision.reason,
                    unresolved_issues,
                    verification,
                    edit_result,
                    coverage,
                    failures,
                    rounds_executed,
                    started,
                    final_output=None,
                    issue_map=issue_map,
                )
        except ReviewflowError as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            if not machine.is_terminal():
                machine.transition(Stage.FAILED, reason=str(exc))
            return self._finalize(
                ctx,
                machine,
                WorkflowStatus.FAILED,
                f"{type(exc).__name__}: {exc}",
                unresolved_issues,
                verification,
                edit_result,
                coverage,
                failures,
                rounds_executed,
                started,
            )

    # -- stages ------------------------------------------------------------ #

    async def _parallel_review(
        self,
        ctx: WorkflowContext,
        plan: ReviewPlan,
        ingest: IngestResult,
        assignment: dict[str, list[str]],
    ) -> tuple[list[ReviewerReport], dict[str, str]]:
        """Run every selected reviewer concurrently, each with its own packet.

        Independence is structural: the packets are built from the plan and the
        material only, so no reviewer can see another's conclusions. Concurrency is
        bounded by a semaphore and every call has its own timeout.
        """
        semaphore = asyncio.Semaphore(self.settings.budget.max_reviewer_concurrency)
        reports: list[ReviewerReport] = []
        failed: dict[str, str] = {}

        async def one(role_key: str) -> tuple[str, InvocationResult]:
            agent = self.factory.build_specialist(role_key)
            packet = packets.review_packet(
                ctx,
                ingest=ingest,
                units=plan.units,
                assigned_unit_ids=assignment.get(role_key, []),
                focus=plan.focus,
                round_index=ctx.round_index,
            )
            async with semaphore:
                result = await self._invoke(
                    ctx,
                    agent,
                    packet,
                    role=role_key,
                    stage=Stage.PARALLEL_REVIEW,
                    validator=self._report_validator(role_key, plan.units),
                )
            return role_key, result

        outcomes = await asyncio.gather(*(one(key) for key in assignment), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):  # pragma: no cover - gather guard
                failed["<unknown>"] = f"{type(outcome).__name__}: {outcome}"
                continue
            role_key, result = outcome
            if not result.ok or result.output is None:
                reason = f"[{result.failure_kind.value if result.failure_kind else 'error'}] {result.error}"
                failed[role_key] = reason
                self._record(
                    ctx,
                    EventType.FAILURE,
                    stage=Stage.PARALLEL_REVIEW,
                    agent_role=role_key,
                    outcome=StageOutcome.FAILED,
                    errors=[reason],
                    retry_count=max(0, result.attempts - 1),
                    detail={"failure_kind": result.failure_kind.value if result.failure_kind else None},
                )
                continue
            report, repair = normalize_reviewer_report(result.output, role_key=role_key)
            report = report.model_copy(update={"validation_notes": [*repair.notes, *result.validation_notes]})
            if repair.notes:
                self._record(
                    ctx,
                    EventType.AGENT_CALL,
                    stage=Stage.PARALLEL_REVIEW,
                    agent_role=role_key,
                    outcome=StageOutcome.RETRIED,
                    detail={"validator_repairs": repair.notes},
                )
                ctx.disclosures.extend(repair.notes)
            reports.append(report)
            self._notify(
                "review.report_received",
                reviewer=report.reviewer,
                scope=report.scope,
                overall_assessment=report.overall_assessment,
                issue_count=len(report.issues),
                unresolved_questions=list(report.unresolved_questions),
                issues=[
                    {
                        "id": issue.id,
                        "severity": issue.severity.value,
                        "category": issue.category.value,
                        "location": issue.location,
                        "description": issue.description,
                        "evidence_status": issue.evidence_status.value,
                        "confidence": issue.confidence,
                    }
                    for issue in report.issues
                ],
            )
        return reports, failed

    async def _controlled_edit(
        self,
        ctx: WorkflowContext,
        ingest: IngestResult,
        issue_map: ConsolidatedIssueMap,
        corpus: list[Issue],
        unresolved_findings: list[str],
        round_index: int,
    ) -> tuple[EditResult | None, DiffEvidence | None, str]:
        """Run the single writer under an explicit grant and a lock.

        The grant is minted here, after synthesis, and re-minted (with the current
        hash) if a retry is needed because the report was invalid even though the
        write succeeded.
        """
        accepted_ids = sorted(issue_map.accepted_ids())
        target = ctx.target_path
        relative = target.relative_to(ctx.workspace_root).as_posix()
        # Read bytes then decode: byte-level reads keep the recorded hash authoritative
        # on platforms where text mode would translate line endings.
        before_bytes = target.read_bytes()
        before_text = before_bytes.decode("utf-8", errors="replace")
        editor = self.factory.build_editor()

        async with self._write_lock:
            attempts = 0
            problems: list[str] = []
            edit_result: EditResult | None = None
            diff_sha_before = sha256_bytes(before_bytes)
            while attempts < 2:
                attempts += 1
                ctx.issue_grant(target, sha256_of(target), accepted_ids, round_index=round_index)
                packet = packets.edit_packet(
                    ctx,
                    ingest=self._reingest(ctx, ingest, before_text),
                    issue_map=issue_map,
                    corpus=corpus,
                    unresolved_findings=[*unresolved_findings, *problems],
                    round_index=round_index,
                )
                result = await self._invoke(
                    ctx,
                    editor,
                    packet,
                    role=self.registry.writer().role_key,
                    stage=Stage.CONTROLLED_EDIT,
                    validator=lambda candidate: self._edit_shape_problems(candidate, relative, accepted_ids),
                )
                after_bytes = target.read_bytes()
                after_text = after_bytes.decode("utf-8", errors="replace")
                diff = with_disk_hashes(
                    compute_diff(relative, before_text, after_text),
                    sha_before=diff_sha_before,
                    sha_after=sha256_bytes(after_bytes),
                )
                if not result.ok or result.output is None:
                    problems = [result.error or "the editor did not return a usable result"]
                    self._record(
                        ctx,
                        EventType.FAILURE,
                        stage=Stage.CONTROLLED_EDIT,
                        agent_role=self.registry.writer().role_key,
                        outcome=StageOutcome.FAILED,
                        errors=problems,
                    )
                    if result.failure_kind in {FailureKind.TRANSIENT, FailureKind.TIMEOUT} and attempts < 2:
                        before_bytes = after_bytes
                        before_text = after_text
                        diff_sha_before = sha256_bytes(before_bytes)
                        continue
                    return None, diff, after_text
                edit_result = result.output
                outcome = validate_edit_result(
                    edit_result,
                    granted_target=relative,
                    touched_paths=[relative],
                    # The pre-edit hash, captured before the run: the grant's hash has
                    # already advanced past this attempt's own write by the time we validate.
                    sha_before=diff_sha_before,
                    sha_after=diff.sha_after,
                    allowed_issue_ids=set(accepted_ids),
                    text_before=before_text,
                    text_after=after_text,
                )
                if outcome.ok:
                    return edit_result, diff, after_text
                problems = outcome.problems
                ctx.disclosures.extend(outcome.notes)
                self._record(
                    ctx,
                    EventType.FAILURE,
                    stage=Stage.CONTROLLED_EDIT,
                    agent_role=self.registry.writer().role_key,
                    outcome=StageOutcome.INVALID_OUTPUT,
                    errors=problems,
                    retry_count=attempts,
                )
                before_bytes = after_bytes
                before_text = after_text
                diff_sha_before = sha256_bytes(before_bytes)
            return (
                edit_result,
                with_disk_hashes(
                    compute_diff(relative, before_text, after_text),
                    sha_before=diff_sha_before,
                    sha_after=sha256_bytes(target.read_bytes()),
                ),
                target.read_bytes().decode("utf-8", errors="replace"),
            )

    async def _semantic_verification(
        self,
        ctx: WorkflowContext,
        ingest: IngestResult,
        issue_map: ConsolidatedIssueMap,
        corpus: list[Issue],
        diff: DiffEvidence,
        runner: VerificationRunner,
    ) -> list[Any]:
        """Ask the verifier agent for the semantic checks (one call, all checks)."""
        agent = self.factory.build_verifier()
        packet = packets.verification_packet(
            ctx,
            ingest=ingest,
            checks=runner.semantic_check_payload(),
            issue_map=issue_map,
            corpus=corpus,
            diff_text=diff.unified_diff,
        )
        result = await self._invoke(ctx, agent, packet, role="verifier", stage=Stage.VERIFY)
        if not result.ok or result.output is None:
            self._record(
                ctx,
                EventType.FAILURE,
                stage=Stage.VERIFY,
                agent_role="verifier",
                outcome=StageOutcome.FAILED,
                errors=[result.error or "verifier failed"],
            )
            ctx.note_failed_role("verifier", result.error or "semantic verification failed")
            return [
                self._failed_semantic_result(name, f"verifier failed: {result.error}")
                for name in runner.semantic_checks
            ]
        return runner.semantic_results(result.output)

    @staticmethod
    def _failed_semantic_result(name: str, error: str) -> Any:
        from reviewflow.models.contracts import VerificationResult
        from reviewflow.models.enums import VerificationKind

        return VerificationResult(
            check=name,
            command_or_method="semantic check requested from the verifier agent",
            passed=False,
            evidence="",
            error=error,
            kind=VerificationKind.SEMANTIC,
            mandatory=True,
        )

    async def _final_review(
        self,
        ctx: WorkflowContext,
        machine: StateMachine,
        *,
        status: WorkflowStatus,
        reason: str,
        ingest: IngestResult,
        issue_map: ConsolidatedIssueMap,
        corpus: list[Issue],
        verification: VerificationReport,
        verdict: AdversarialVerdict | None,
        diff_review: DiffReviewResult | None,
        diff: DiffEvidence,
        unresolved_issues: list[UnresolvedIssue],
        coverage: CoverageTracker,
        failures: list[str],
        rounds_executed: int,
        started: float,
    ) -> WorkflowResult:
        """Produce the user-facing report, with the computed status as the authority."""
        ctx.stage = Stage.FINAL_REVIEW
        ctx.computed_status = status
        self._stage_start(ctx, machine, Stage.FINAL_REVIEW)
        packet = packets.final_packet(
            ctx,
            ingest=ingest,
            issue_map=issue_map,
            corpus=corpus,
            verification=verification,
            adversarial=verdict,
            diff_review=diff_review,
            diff_text=diff.unified_diff,
            computed_status=status.value,
            status_reason=reason,
            failures=failures,
            rounds_executed=rounds_executed,
            coverage=coverage.resolve().model_dump(),
        )
        result = await self._invoke(
            ctx, self.factory.build_final_reporter(), packet, role="orchestrator", stage=Stage.FINAL_REVIEW
        )
        final_output: FinalReport | None = result.output if result.ok else None
        if not result.ok:
            failures.append(f"final report generation failed: {result.error}")
            ctx.disclosures.append("the final report was authored by the workflow, not by the orchestrator, after a failure")
            final_output = self._fallback_report(status, reason, issue_map, unresolved_issues, failures)
        elif final_output is not None and final_output.claimed_status is not status:
            # The output guardrail only trips on *inflation*; a more cautious claim is allowed
            # but is recorded so the discrepancy is traceable.
            ctx.disclosures.append(
                f"orchestrator claimed status {final_output.claimed_status.value!r}; the computed status is {status.value!r}"
            )
        self._stage_end(ctx, Stage.FINAL_REVIEW, StageOutcome.OK if result.ok else StageOutcome.FAILED)

        machine.transition(Stage.COMPLETE, reason="final report produced")
        return self._finalize(
            ctx,
            machine,
            status,
            reason,
            unresolved_issues,
            verification,
            None,
            coverage,
            failures,
            rounds_executed,
            started,
            final_output=final_output,
        )

    # -- helpers ----------------------------------------------------------- #

    async def _invoke(
        self,
        ctx: WorkflowContext,
        agent: Agent[Any],
        input_text: str,
        *,
        role: str,
        stage: Stage,
        validator: Callable[[Any], ValidationOutcome] | None = None,
        max_turns: int | None = None,
    ) -> InvocationResult:
        """Invoke one agent, record the attempt in the ledger, and accumulate usage."""
        request = InvocationRequest(
            agent=agent,
            role_key=role,
            stage=stage,
            context=ctx,
            input_text=input_text,
            round_index=ctx.round_index,
            validator=validator,
            max_turns=max_turns,
        )
        result = await self.invoker.invoke(request)
        ctx.record_usage(role, result.input_tokens, result.output_tokens, result.latency_ms)
        self._record(
            ctx,
            EventType.AGENT_CALL,
            stage=stage,
            agent_role=role,
            agent_name=agent.name,
            outcome=StageOutcome.OK if result.ok else StageOutcome.FAILED,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=result.model,
            retry_count=max(0, result.attempts - 1),
            errors=[result.error] if result.error else [],
            detail={"failure_kind": result.failure_kind.value if result.failure_kind else None},
        )
        return result

    @staticmethod
    def _edit_shape_problems(candidate: EditResult, relative: str, accepted_ids: list[str]) -> ValidationOutcome:
        """Checks that need only the editor's report (facts about the disk are checked later)."""
        out = ValidationOutcome()
        if candidate.target != relative:
            out.problems.append(f"edit.target {candidate.target!r} is not the granted target {relative!r}")
        cited = {issue_id for change in candidate.changes for issue_id in change.issue_ids}
        unknown = sorted(cited - set(accepted_ids))
        if unknown:
            out.problems.append(f"changes cite issue ids outside the accepted set: {unknown}")
        return out

    def _reingest(self, ctx: WorkflowContext, ingest: IngestResult, text: str) -> IngestResult:
        """Rebuild the material view after an edit so packets show current bytes."""
        return IngestResult(
            relative_path=ingest.relative_path,
            text=text,
            sha256=sha256_of(ctx.target_path),
            line_count=text.count("\n") + 1,
            char_count=len(text),
            truncated=ingest.truncated,
            unit_hints=ingest.unit_hints,
            warnings=ingest.warnings,
        )

    @staticmethod
    def _reconcile_corpus(
        issue_map: ConsolidatedIssueMap, corpus: list[Issue], notes: list[str]
    ) -> list[Issue]:
        """Align the corpus with the map, dropping nothing.

        Validation guarantees the map is a partition of the corpus, so this only
        re-orders for readability and keeps validation notes attached.
        """
        if not notes:
            return corpus
        return corpus

    @staticmethod
    def _verdict_findings(verdict: AdversarialVerdict) -> list[str]:
        findings: list[str] = []
        for bucket, label in ((verdict.critical_issues, "critical"), (verdict.major_issues, "major"), (verdict.minor_issues, "minor")):
            for finding in bucket:
                findings.append(f"adversarial ({label}): {finding.description} @ {finding.location}")
        findings.extend(f"adversarial rejection reason: {reason}" for reason in verdict.rejection_reasons)
        return findings

    @staticmethod
    def _resolved_issue_ids(
        issue_map: ConsolidatedIssueMap, edit_result: EditResult | None, diff: DiffEvidence | None
    ) -> set[str]:
        """Accepted action-required ids that a *real byte change* actually cites.

        An issue counts as resolved only when the diff is non-empty and the editor's
        reported change cites it. The editor's report is checked against the diff by
        :func:`validate_edit_result`, and the semantic verifier independently judges
        whether the change implements the fix.
        """
        if edit_result is None or diff is None or not diff.changed:
            return set()
        cited = {issue_id for change in edit_result.changes for issue_id in change.issue_ids}
        return cited & issue_map.accepted_ids()

    @staticmethod
    def _unresolved_from_map(
        issue_map: ConsolidatedIssueMap, corpus: list[Issue], resolved_ids: set[str]
    ) -> list[UnresolvedIssue]:
        by_id = {issue.id: issue for issue in corpus}
        out: list[UnresolvedIssue] = []
        for resolution in issue_map.accepted_issues:
            issue = by_id.get(resolution.issue_id)
            if issue is None or not resolution.action_required:
                continue
            if resolution.issue_id in resolved_ids:
                continue
            out.append(
                UnresolvedIssue(
                    issue_id=issue.id,
                    summary=issue.description,
                    severity=issue.severity,
                    source=issue.source_reviewer or "reviewer",
                    reason="accepted and action required; resolved only if a verified edit cites it",
                )
            )
        for conflict in issue_map.conflicts:
            if conflict.adjudicated:
                continue
            out.append(
                UnresolvedIssue(
                    issue_id=",".join(conflict.issue_ids) or "conflict",
                    summary=conflict.summary,
                    severity=Severity.MAJOR,
                    source="synthesis",
                    reason=f"unresolved reviewer disagreement: {conflict.reason}",
                )
            )
        return out

    @staticmethod
    def _unresolved_from_gates(
        verdict: AdversarialVerdict | None,
        verification: VerificationReport | None,
        findings: list[str],
    ) -> list[UnresolvedIssue]:
        out: list[UnresolvedIssue] = []
        if verdict is not None:
            for finding in [*verdict.critical_issues, *verdict.major_issues]:
                out.append(
                    UnresolvedIssue(
                        issue_id=finding.related_issue_id or "adversarial",
                        summary=finding.description,
                        severity=Severity.CRITICAL if finding in verdict.critical_issues else Severity.MAJOR,
                        source="adversarial_reviewer",
                        reason=finding.evidence,
                    )
                )
        if verification is not None:
            for result in verification.mandatory_failures:
                out.append(
                    UnresolvedIssue(
                        issue_id=f"verification:{result.check}",
                        summary=f"verification check {result.check} failed",
                        severity=Severity.CRITICAL,
                        source="verification",
                        reason=result.error or result.evidence or "check failed",
                    )
                )
        for note in findings:
            if any(item.summary == note for item in out):
                continue
            out.append(
                UnresolvedIssue(
                    issue_id="open-finding",
                    summary=note,
                    severity=Severity.MINOR,
                    source="workflow",
                    reason="unresolved at completion",
                )
            )
        return out

    def _report_validator(
        self, role_key: str, units: list[ReviewUnit]
    ) -> Callable[[Any], ValidationOutcome]:
        """The reviewer checks bound to this agent and this plan's unit ids."""
        known_units = {unit.unit_id for unit in units}

        def validate(candidate: Any) -> ValidationOutcome:
            return check_reviewer_report(candidate, role_key=role_key, known_unit_ids=known_units)

        return validate

    def _issue_map_validator(self, corpus: list[Issue]) -> Callable[[Any], ValidationOutcome]:
        """A validator bound to the corpus revision it was built for."""
        issues = tuple(corpus)
        known_roles = self.registry.keys()

        def validate(candidate: Any) -> ValidationOutcome:
            return validate_issue_map(candidate, corpus=list(issues), known_roles=known_roles)

        return validate

    def _verdict_validator(self, corpus: list[Issue]) -> Callable[[Any], ValidationOutcome]:
        """A verdict validator bound to the issue ids that exist in this revision."""
        allowed = frozenset(issue.id for issue in corpus)

        def validate(candidate: Any) -> ValidationOutcome:
            return validate_adversarial_verdict(candidate, allowed_issue_ids=set(allowed))

        return validate

    def _record_verification(self, ctx: WorkflowContext, report: VerificationReport) -> ValidationOutcome:
        from reviewflow.validation import validate_verification_report

        for result in report.results:
            self._record(
                ctx,
                EventType.VERIFICATION,
                stage=Stage.VERIFY,
                outcome=StageOutcome.OK if result.passed else StageOutcome.FAILED,
                verification_status=f"{result.check}:{'pass' if result.passed else 'fail'}",
                errors=[result.error] if result.error else [],
                detail={"check": result.check, "kind": result.kind.value, "mandatory": result.mandatory},
            )
        return validate_verification_report(report)

    def _stage_start(self, ctx: WorkflowContext, machine: StateMachine, stage: Stage) -> None:
        ctx.stage = stage
        self._record(ctx, EventType.STAGE_START, stage=stage, detail={"round": ctx.round_index})

    def _stage_end(
        self,
        ctx: WorkflowContext,
        stage: Stage,
        outcome: StageOutcome,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._record(ctx, EventType.STAGE_END, stage=stage, outcome=outcome, detail=detail or {})

    def _notify(self, event: str, **payload: Any) -> None:
        """Publish a milestone through the optional event sink.

        Used for facts a workflow observer cares about that are not already ledger events
        (a reviewer's findings, the shape of the issue map, whether an edit changed
        anything). No sink means no work and no behaviour change; a failing sink is logged
        rather than allowed to abort a run, because observability must never be able to
        break the thing it observes.
        """
        if self.event_sink is None:
            return
        try:
            self.event_sink(event, payload)
        except Exception:
            _log.warning("event sink raised", extra={"role": "orchestrator", "event": event})

    @staticmethod
    def _capture(ctx: WorkflowContext, key: str, value: Any) -> None:
        """Record a stage's structured output as durable evidence.

        Handles all three shapes the engine produces: Pydantic contracts (dump to JSON),
        plain dataclasses such as ``DiffEvidence`` (asdict - *not* ``str()``, which would
        silently store a repr where a structure belongs), and everything else as-is so the
        JSON serialiser complains at write time rather than dropping evidence.

        Capture never raises: a run must not fail because its evidence could not be recorded.
        """

        def convert(item: Any) -> Any:
            if hasattr(item, "model_dump"):
                return item.model_dump(mode="json")
            if is_dataclass(item) and not isinstance(item, type):
                return asdict(item)
            if isinstance(item, (list, tuple)):
                return [convert(element) for element in item]
            if isinstance(item, dict):
                return {name: convert(element) for name, element in item.items()}
            return item

        try:
            ctx.stage_outputs[key] = convert(value)
        except Exception as exc:
            # The key and reason go in the message, not only in `extra`: a log formatter
            # that drops extra fields would otherwise turn this into an unactionable
            # one-liner, and an evidence write that fails silently is how a run ends up
            # with no inspectable record and nobody noticing.
            _log.warning(
                f"stage output capture failed for '{key}': {type(exc).__name__}: {exc}"[:300],
                extra={"role": "orchestrator", "key": key},
            )

    def _record(
        self,
        ctx: WorkflowContext,
        event_type: EventType,
        *,
        stage: Stage | None = None,
        agent_role: str | None = None,
        agent_name: str | None = None,
        outcome: StageOutcome | None = None,
        errors: list[str] | None = None,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        model: str | None = None,
        retry_count: int = 0,
        verification_status: str | None = None,
        issue_ids: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.ledger.record(
            AuditEvent(
                workflow_id=ctx.workflow_id,
                event_type=event_type,
                stage=stage,
                agent_role=agent_role,
                agent_name=agent_name,
                outcome=outcome,
                errors=errors or [],
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                retry_count=retry_count,
                verification_status=verification_status,
                issue_ids=issue_ids or [],
                round_index=ctx.round_index,
                detail={k: v for k, v in (detail or {}).items() if v is not None},
            )
        )

    def _fail(
        self,
        ctx: WorkflowContext,
        machine: StateMachine,
        stage: Stage,
        result: InvocationResult,
        failures: list[str],
        started: float,
    ) -> WorkflowResult:
        message = f"{result.role_key} failed during {stage.value}: {result.error}"
        failures.append(message)
        self._record(
            ctx,
            EventType.FAILURE,
            stage=stage,
            agent_role=result.role_key,
            outcome=StageOutcome.FAILED,
            errors=[result.error or ""],
        )
        machine.transition(Stage.FAILED, reason=message)
        coverage = CoverageTracker()
        return self._finalize(ctx, machine, WorkflowStatus.FAILED, message, [], None, None, coverage, failures, 0, started)

    def _finalize(
        self,
        ctx: WorkflowContext,
        machine: StateMachine,
        status: WorkflowStatus,
        reason: str,
        unresolved_issues: list[UnresolvedIssue],
        verification: VerificationReport | None,
        edit_result: EditResult | None,
        coverage: CoverageTracker,
        failures: list[str],
        rounds_executed: int,
        started: float,
        *,
        final_output: FinalReport | None = None,
        issue_map: ConsolidatedIssueMap | None = None,
    ) -> WorkflowResult:
        if final_output is None:
            # Blocked and failed runs still owe the user an honest report. This one is
            # authored by the workflow itself, so it cannot overstate the status.
            final_output = self._fallback_report(status, reason, issue_map, unresolved_issues, failures)
        usage = ctx.usage
        usage.wall_clock_ms = int((time.monotonic() - started) * 1000)
        self._capture(ctx, "final_report", final_output)
        self._capture(ctx, "review_coverage", coverage.resolve())
        self._capture(ctx, "unresolved_issues", unresolved_issues)
        self._capture(ctx, "usage", usage)
        ctx.stage_outputs.setdefault("terminal", {})
        if isinstance(ctx.stage_outputs["terminal"], dict):
            ctx.stage_outputs["terminal"].update(
                {"status": status.value, "reason": reason, "rounds_executed": rounds_executed, "failures": failures}
            )
        artifacts: dict[str, str] = {}
        result = WorkflowResult(
            workflow_id=ctx.workflow_id,
            status=status,
            status_reason=reason,
            stages_completed=machine.stages_completed(),
            rounds_executed=rounds_executed,
            unresolved_issues=unresolved_issues,
            verification_results=verification.results if verification else [],
            final_output=final_output,
            review_coverage=coverage.resolve(),
            usage=usage,
            failures=failures,
            artifacts=artifacts,
        )
        self.ledger.record(
            AuditEvent(
                workflow_id=ctx.workflow_id,
                event_type=EventType.RUN_FINALIZED,
                stage=machine.stage,
                outcome=StageOutcome.OK if status is WorkflowStatus.SUCCEEDED else StageOutcome.FAILED,
                detail={"status": status.value, "reason": reason, "failures": failures},
            )
        )
        artifacts = self._write_artifacts(ctx)
        result.artifacts = artifacts
        self.ledger.finalize(
            WorkflowRunRecord(
                workflow_id=ctx.workflow_id,
                task_id=ctx.task_id,
                target=ctx.target_path.as_posix(),
                objective=ctx.objective,
                status=status.value,
                status_reason=reason,
                rounds_executed=rounds_executed,
                stages_completed=[s.value for s in machine.stages_completed()],
                total_tokens=usage.total_tokens,
                model_calls=usage.model_calls,
                failed_roles=coverage.failed,
                result_json=json.loads(result.model_dump_json()),
            )
        )
        _log.info(
            "workflow finished",
            extra={"workflow_id": ctx.workflow_id, "stage": machine.stage.value, "role": "orchestrator"},
        )
        return result

    def _write_artifacts(self, ctx: WorkflowContext) -> dict[str, str]:
        """Persist the run's evidence next to the ledger so it outlives the session."""
        directory = self.settings.ledger_path.parent / ctx.workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        events_path = directory / "audit_events.json"
        events_path.write_text(
            json.dumps([e.model_dump(mode="json") for e in self.ledger.events(ctx.workflow_id)], indent=2),
            encoding="utf-8",
        )
        # Structured stage outputs are the run's inspectable evidence: they are what a
        # UI (or a later reviewer) reads instead of re-running the workflow.
        outputs_path = directory / "stage_outputs.json"
        outputs_path.write_text(
            json.dumps(ctx.stage_outputs, indent=2, default=str),
            encoding="utf-8",
        )
        return {"audit_events": str(events_path), "stage_outputs": str(outputs_path)}

    @staticmethod
    def _fallback_report(
        status: WorkflowStatus,
        reason: str,
        issue_map: ConsolidatedIssueMap | None,
        unresolved: list[UnresolvedIssue],
        failures: list[str],
    ) -> FinalReport:
        """A code-authored report used when the orchestrator cannot produce one.

        It never claims more than the computed status, which is the point: if the
        report agent fails, the workflow still reports honestly.
        """
        return FinalReport(
            executive_summary=(
                f"The workflow finished with status '{status.value}'. The orchestrator could not produce its "
                f"report, so this summary was written by the workflow itself. Reason: {reason}"
            ),
            accepted_issues_summary=[
                f"{r.issue_id}: {r.rationale}" for r in (issue_map.accepted_issues if issue_map else [])
            ],
            rejected_issues_summary=[
                f"{r.issue_id}: {r.rationale}" for r in (issue_map.rejected_issues if issue_map else [])
            ],
            reviewer_disagreements=[c.summary for c in (issue_map.conflicts if issue_map else [])],
            changes_applied=[],
            unresolved_issues=[f"{i.issue_id}: {i.summary} ({i.reason})" for i in unresolved],
            verification_summary=[],
            disclosures=["this report was generated by the workflow after an agent failure", *failures],
            claimed_status=status,
        )
