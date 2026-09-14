"""Run service: turning a stored request into an executed ReviewFlow workflow.

This is the only module that knows both languages - the application's (projects, sources,
runs) and the engine's (Settings, ReviewWorkflow, Ledger) - which is what keeps the API
layer thin and the engine untouched.

Two decisions worth stating plainly:

* **The engine's terminal status is never rewritten.** ``succeeded``/``degraded``/
  ``blocked``/``failed`` come from the engine's own gates and are stored verbatim. The
  application adds only ``cancelled`` (the engine has no such state) for a run the user
  stopped, and documents that mapping rather than dressing a cancellation up as a failure.
* **A run's workspace is derived from the run id**, never from request data, and it is the
  only root the engine is granted. The uploaded source is copied byte-for-byte into it, so
  the original stays immutable and the editor can only ever touch the copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from reviewflow.agents.registry import SpecialistRegistry, build_default_registry
from reviewflow.config import Budget, ModelsByRole, RetryLimits, Settings, TracingSettings
from reviewflow.evals.scenarios import build_scenarios
from reviewflow.observability import get_logger
from reviewflow.orchestration.workflow import ReviewWorkflow
from reviewflow.persistence import open_ledger
from reviewflow.providers.routing import ModelRouter
from reviewflow.simulate import ScriptedModel, ScriptedModelProvider
from reviewflow.testkit import DEFAULT_MATERIAL
from reviewflow_app.config import AppSettings
from reviewflow_app.errors import AppError, ConflictError, NotFoundError, ValidationError
from reviewflow_app.events import EventBus, PublishingLedger, RunRecorder
from reviewflow_app.persistence.db import Database
from reviewflow_app.persistence.repositories import (
    ArtifactRepository,
    EventRepository,
    ProjectRepository,
    RunRepository,
    SourceRepository,
    new_id,
)
from reviewflow_app.schemas import (
    TERMINAL_STATUSES,
    ArtifactRef,
    RunConfigSnapshot,
    RunCreateRequest,
    RunInspection,
    StageState,
    StageStateName,
    WorkflowRunSummary,
)
from reviewflow_app.security.paths import assert_within, atomic_write_bytes, sanitize_filename
from reviewflow_app.tracing import install_trace_router

_log = get_logger("app.runs")

#: The stages the timeline renders, in engine order. Kept here (not in the UI) so the
#: frontend has no opinion about workflow structure.
STAGE_ORDER: list[str] = [
    "ingest",
    "plan",
    "parallel_review",
    "synthesize",
    "controlled_edit",
    "diff_review",
    "adversarial_review",
    "verify",
    "final_review",
]

STAGE_LABELS: dict[str, str] = {
    "ingest": "Ingest",
    "plan": "Plan",
    "parallel_review": "Parallel review",
    "synthesize": "Synthesize",
    "controlled_edit": "Controlled edit",
    "diff_review": "Diff review",
    "adversarial_review": "Adversarial review",
    "verify": "Verify",
    "revise": "Revise",
    "final_review": "Final review",
    "complete": "Complete",
    "blocked": "Blocked",
    "failed": "Failed",
}


class SourceService:
    """Uploaded source material: immutable, hashed, and never edited in place."""

    def __init__(self, database: Database, app_settings: AppSettings) -> None:
        self.db = database
        self.app_settings = app_settings
        self.sources = SourceRepository(database)
        self.projects = ProjectRepository(database)

    def upload(
        self,
        *,
        project_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            raise NotFoundError(f"project {project_id} does not exist")
        if not data:
            raise ValidationError("the uploaded file is empty")
        if len(data) > self.app_settings.max_upload_bytes:
            raise ValidationError(
                f"file is {len(data)} bytes, over the {self.app_settings.max_upload_bytes} byte limit"
            )
        safe_name = sanitize_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in self.app_settings.allowed_source_suffixes:
            raise ValidationError(
                f"'{suffix or safe_name}' is not an accepted source type "
                f"({', '.join(self.app_settings.allowed_source_suffixes)})"
            )

        # The generated id decides the directory and the sanitised name only decides the leaf
        # filename, so nothing a client sends can move the write outside the project - and,
        # critically, two uploads of the same name cannot collide. Deriving the directory from
        # the filename (the previous behaviour) made the second upload overwrite the first
        # snapshot while both database rows claimed their own path, which broke the immutable
        # snapshot guarantee the audit trail depends on.
        source_id = new_id("src")
        directory = self.app_settings.source_dir(project_id, source_id)
        directory.mkdir(parents=True, exist_ok=True)
        stored = assert_within(directory / safe_name, self.app_settings.projects_dir)
        atomic_write_bytes(stored, data)

        record = self.sources.create(
            project_id=project_id,
            filename=safe_name,
            stored_path=str(stored),
            source_id=source_id,
            content_type=content_type or "application/octet-stream",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self.projects.touch(project_id)
        return record

    def read_bytes(self, source_id: str) -> bytes:
        record = self.sources.get(source_id)
        if record is None:
            raise NotFoundError(f"source {source_id} does not exist")
        path = assert_within(Path(record["stored_path"]), self.app_settings.projects_dir)
        if not path.is_file():
            raise NotFoundError(f"source {source_id} is missing from disk")
        return path.read_bytes()

    def preview(self, source_id: str, *, limit: int = 1200) -> str | None:
        """A short, safe preview. Only text-like sources have one."""
        record = self.sources.get(source_id)
        if record is None:
            return None
        if Path(record["filename"]).suffix.lower() not in {".md", ".markdown", ".txt", ".rst", ".py", ".csv", ".json", ".yml", ".yaml"}:
            return None
        try:
            text = self.read_bytes(source_id).decode("utf-8", errors="replace")
        except (NotFoundError, OSError):
            return None
        return text[:limit]

    def samples(self) -> list[dict[str, Any]]:
        """Deterministic sample material a user can load to try the application.

        Every sample is named ``manuscript.md`` because the engine's scripted evaluation
        scenarios are authored against that filename: their canned edit calls name the
        target path explicitly, so a scripted run over a differently named file would
        (correctly) find nothing to change and end blocked. Naming the sample the same way
        is what makes "Try a sample + scripted engine" a genuinely deterministic
        demonstration rather than a lucky one.
        """
        samples = [
            {
                "id": "sample_manuscript",
                "name": "manuscript.md",
                "description": "Short research abstract containing a genuine unsupported claim.",
                "material": DEFAULT_MATERIAL,
            }
        ]
        for scenario in build_scenarios():
            if scenario.material:
                samples.append(
                    {
                        "id": f"scenario:{scenario.name}",
                        "name": "manuscript.md",
                        "description": f"Scripted scenario material: {scenario.description}",
                        "material": scenario.material,
                    }
                )
        return samples

    def upload_sample(self, *, project_id: str, sample_id: str) -> dict[str, Any]:
        for sample in self.samples():
            if sample["id"] == sample_id:
                return self.upload(
                    project_id=project_id,
                    filename=str(sample["name"]),
                    content_type="text/markdown",
                    data=str(sample["material"]).encode("utf-8"),
                )
        raise NotFoundError(f"sample {sample_id} does not exist")


class RunService:
    """Creates, executes, cancels and explains workflow runs."""

    def __init__(self, database: Database, app_settings: AppSettings, bus: EventBus) -> None:
        self.db = database
        self.app_settings = app_settings
        self.bus = bus
        self.runs = RunRepository(database)
        self.events = EventRepository(database)
        self.artifacts = ArtifactRepository(database)
        self.projects = ProjectRepository(database)
        self.sources = SourceRepository(database)
        self.source_service = SourceService(database, app_settings)

    # -- creation ------------------------------------------------------------- #

    def create(self, request: RunCreateRequest) -> dict[str, Any]:
        project = self.projects.get(request.project_id)
        if project is None:
            raise NotFoundError(f"project {request.project_id} does not exist")
        source = self.sources.get(request.source_id)
        if source is None or source["project_id"] != request.project_id:
            raise NotFoundError(f"source {request.source_id} does not belong to project {request.project_id}")

        config = request.config
        if config.mode == "scripted" and not self.app_settings.allow_scripted_runs:
            raise ConflictError("scripted runs are disabled on this deployment")
        if config.mode == "live" and not self.app_settings.allow_live_runs:
            raise ConflictError("live runs are disabled on this deployment")

        run = self.runs.create(
            project_id=request.project_id,
            source_id=request.source_id,
            objective=request.objective,
            mode=config.mode,
            config=config.model_dump(mode="json"),
            workspace_root=str(self.app_settings.run_workspace("pending")),
            target_path="",
        )
        run_id = str(run["id"])
        workspace = self.app_settings.run_workspace(run_id)
        workspace.mkdir(parents=True, exist_ok=True)
        target_name = sanitize_filename(str(source["filename"]), fallback="target.md")
        target = assert_within(workspace / target_name, workspace)
        # Byte-for-byte copy: the uploaded source stays immutable and the engine hashes
        # exactly the bytes a reviewer saw.
        target.write_bytes(self.source_service.read_bytes(str(source["id"])))
        self.runs.annotate(run_id, workspace_root=str(workspace), target_path=str(target))
        return self.require(run_id)

    def require(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} does not exist")
        return run

    # -- engine wiring -------------------------------------------------------- #

    def build_settings(self, run: dict[str, Any]) -> Settings:
        """Engine settings for one run, built from its stored configuration snapshot."""
        config = RunConfigSnapshot.model_validate(run["config"])
        limits = config.limits
        workspace = Path(run["workspace_root"])
        models = ModelsByRole()
        # Only non-`auto` role selections reach the engine; `auto` means "let the router
        # decide for this provider", which is the default ModelsByRole already expresses.
        overrides = dict(config.role_models)
        return Settings(
            workspace_root=workspace,
            allowed_write_roots=[workspace],
            ledger_path=Path("artifacts") / "audit.sqlite3",
            provider=config.provider,
            model_overrides=overrides,
            models=models,
            budget=Budget(
                max_revision_rounds=limits.max_revision_rounds,
                max_turns_per_agent=limits.max_turns_per_agent,
                max_reviewer_concurrency=limits.max_reviewer_concurrency,
                agent_timeout_s=limits.agent_timeout_s,
                max_total_tokens=limits.max_total_tokens,
                retries=RetryLimits(
                    transient=limits.max_transient_retries,
                    invalid_output=limits.max_invalid_output_retries,
                ),
            ),
            tracing=TracingSettings(enabled=True, to_ledger=True),
            min_successful_reviewers=limits.min_successful_reviewers,
            semantic_checks=limits.semantic_checks,
        ).resolved()

    def build_registry(self, config: RunConfigSnapshot) -> SpecialistRegistry:
        """The specialist registry for a run, honouring the requested reviewer set.

        Only *planner-selectable* roles are removable (today: the independent reviewers).
        The editor, the diff reviewer, the adversarial reviewer and the verifier are never
        removable, because a run without them would lose its single writer, its
        falsification step or its verification - the properties the design exists to
        protect.

        The engine classifies its four reviewers as mandatory, and a set that omits one is
        rejected here with the role named rather than silently degraded at the coverage
        gate: an operator who asked for three reviewers deserves to be told why they get
        four, not to discover it in a ``degraded`` status later.
        """
        registry = build_default_registry()
        selectable = {spec.role_key for spec in registry.planner_selectable()}
        mandatory = set(registry.mandatory_keys())
        if not config.reviewers:
            return registry

        requested = {role.strip() for role in config.reviewers if role.strip()}
        unknown = sorted(requested - set(registry.specs))
        if unknown:
            raise ValidationError(f"unknown reviewer roles requested: {', '.join(unknown)}")
        not_selectable = sorted(requested - selectable)
        if not_selectable:
            raise ValidationError(f"roles that cannot be chosen per run: {', '.join(not_selectable)}")

        removed_mandatory = sorted(mandatory - requested)
        if removed_mandatory:
            raise ValidationError(
                "the engine requires these reviewers on every run: "
                + ", ".join(removed_mandatory)
                + ". They cannot be disabled."
            )

        removable = selectable - mandatory
        keep = set(registry.specs) - (removable - requested)
        specs = {key: spec for key, spec in registry.specs.items() if key in keep}
        return SpecialistRegistry(specs=specs, policies=registry.policies)

    def _scripted_provider(self, run: dict[str, Any]) -> ScriptedModelProvider:
        config = RunConfigSnapshot.model_validate(run["config"])
        for scenario in build_scenarios():
            if scenario.name == config.scripted_scenario:
                return ScriptedModelProvider(ScriptedModel(scenario.scripts()))
        available = ", ".join(scenario.name for scenario in build_scenarios())
        raise ValidationError(f"unknown scripted scenario '{config.scripted_scenario}'; available: {available}")

    # -- execution ------------------------------------------------------------ #

    async def execute(self, run_id: str) -> None:
        """Run the workflow. Called by the executor, never by a request handler."""
        run = self.require(run_id)
        config = RunConfigSnapshot.model_validate(run["config"])
        recorder = RunRecorder.for_run(run_id, self.db, self.bus)
        router: ModelRouter | None = None
        if config.mode == "live":
            router = ModelRouter.from_settings(self.build_settings(run).model_copy(update={"provider": config.provider}))
            if router is None:
                self._finish(
                    run_id,
                    status="failed",
                    reason=(
                        f"provider '{config.provider}' has no usable credential; "
                        "no model calls were made"
                    ),
                    recorder=recorder,
                )
                return

        settings = self.build_settings(run)
        registry = self.build_registry(config)
        def sink(event: str, detail: dict[str, Any]) -> None:
            """Engine notifications -> application events.

            Typed as returning None deliberately: the engine treats the sink as a
            fire-and-forget observer, and a sink that returned a value would invite the
            engine to start caring about the UI's bookkeeping.
            """
            recorder.from_sink(event, detail)

        ledger = open_ledger(settings.ledger_path)
        publishing = PublishingLedger(
            ledger,
            recorder,
            # Keep the run row's stage current, so a dashboard or a history list shows real
            # progress even when nothing is watching the event stream.
            on_stage=lambda stage: self.runs.set_stage(run_id, stage),
        )
        provider: Any = router if config.mode == "live" else self._scripted_provider(run)

        workflow = ReviewWorkflow(
            settings,
            ledger=publishing,
            registry=registry,
            model_provider=provider,
            event_sink=sink,
        )
        engine_workflow_id = ""  # assigned by the engine; we copy it after the run starts
        trace_router = install_trace_router()
        effective_provider = router.default_provider if router else "scripted"
        self.runs.mark_started(
            run_id,
            engine_workflow_id=run_id,
            provider_snapshot={
                "mode": config.mode,
                "provider": effective_provider,
                "requested_provider": config.provider,
                "role_models": config.role_models,
                "transport": (
                    "streaming" if router and router.requires_streaming_for(router.model_for_role("orchestrator")) else "standard"
                ),
            },
        )
        recorder.emit(
            "workflow.started",
            payload={"mode": config.mode, "provider": effective_provider, "objective": run["objective"]},
        )

        try:
            result = await workflow.run(
                objective=run["objective"],
                target=Path(run["target_path"]),
                constraints=None,
                user_preferences=None,
                verification_config=None,
            )
        except asyncio.CancelledError:
            # Cancellation is its own terminal state. It is never reported as a failure,
            # because a run the user stopped did not fail - and the engine never claims
            # success for it either.
            self._finish(
                run_id,
                status="cancelled",
                reason="the run was cancelled by the user; no further stages were scheduled",
                recorder=recorder,
            )
            raise
        except Exception as exc:
            self._finish(
                run_id,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
                recorder=recorder,
                engine_workflow_id=engine_workflow_id,
            )
            _log.error("run failed", extra={"workflow_id": run_id, "role": "app", "error": str(exc)[:400]})
            return
        finally:
            trace_router.unregister(run_id)
            ledger.close()

        engine_workflow_id = result.workflow_id
        self._register_artifacts(run_id, run, result)
        self._finish(
            run_id,
            status=result.status.value,
            reason=result.status_reason,
            recorder=recorder,
            usage=self._usage_payload(result),
            result=result.model_dump(mode="json"),
            engine_workflow_id=engine_workflow_id,
            rounds=result.rounds_executed,
        )

    async def record_unhandled_failure(self, run_id: str, exc: BaseException) -> None:
        """Persist a truthful failure for a run whose executor escaped before finishing.

        Without this, a crash between "started" and the first terminal write would leave a
        run in ``running`` for ever: no terminal state, and a live stream that can never
        end. The message names the failure class so the cause is not lost.
        """
        run = self.runs.get(run_id)
        if run is None:
            return
        if str(run["status"]) in TERMINAL_STATUSES:
            return
        recorder = RunRecorder.for_run(run_id, self.db, self.bus)
        self._finish(
            run_id,
            status="failed",
            reason=f"the executor raised before the workflow could finish: {type(exc).__name__}: {exc}",
            recorder=recorder,
        )

    def _usage_payload(self, result: Any) -> dict[str, Any]:
        usage = result.usage
        payload = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage)
        # Cost is deliberately absent rather than guessed: the providers used here do not
        # publish per-token pricing to this application, and inventing a number would be
        # worse than reporting none.
        payload["cost"] = {"amount": None, "currency": None, "label": "unknown"}
        # ``by_role`` is a mapping of role -> RoleUsage; the per-role rows are what the
        # usage panel renders, so the values are flattened out (each row names its role).
        by_role = getattr(usage, "by_role", {}) or {}
        payload["by_role"] = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in by_role.values()
        ]
        return payload

    def _finish(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        recorder: RunRecorder,
        usage: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        engine_workflow_id: str = "",
        rounds: int = 0,
    ) -> None:
        """Persist the terminal state and emit the matching lifecycle event."""
        if status not in TERMINAL_STATUSES:
            raise AppError(f"{status!r} is not a terminal status")
        self.runs.annotate(run_id, engine_workflow_id=engine_workflow_id or run_id, revision_round=rounds)
        self.runs.finish(run_id, status=status, failure_reason=None if status == "succeeded" else reason, usage=usage, result=result)
        recorder.emit(
            f"workflow.{status}",
            payload={"status": status, "reason": reason, "rounds": rounds},
        )
        self.bus.close(run_id)

    def _source_path(self, run: dict[str, Any]) -> Path | None:
        """The immutable source this run ingested, as stored by the source service.

        Read through the sources table rather than trusted from the run row: the path is
        server-owned and the run row is only a reference to it.
        """
        source = self.sources.get(str(run.get("source_id") or ""))
        if not source:
            return None
        stored = source.get("stored_path")
        return Path(str(stored)) if stored else None

    def _register_artifacts(self, run_id: str, run: dict[str, Any], result: Any) -> None:
        """Index every artifact a run produced.

        The set is deliberately the *files* of record rather than one file per stage:
        ``stage_outputs.json`` already carries the engine's own structured contracts for
        every stage (reviewer reports, issue map, diff, adversarial verdict, verification,
        final report), so splitting it into a dozen near-copies would add storage and
        divergence without adding evidence. The immutable source snapshot and the audit
        ledger are included because a reviewer of the run needs the input and the event
        log, not only the output.

        Each candidate is indexed under the root it is legitimately allowed to live in -
        the run's workspace for documents the engine produced, the project's source store
        for the ingested snapshot - and a failure to index one artifact is logged, never
        allowed to change the run's terminal state. The engine's verified verdict is not
        something bookkeeping gets to rewrite.
        """
        workspace = Path(run["workspace_root"])
        candidates: list[tuple[str, Path, Path]] = []
        target = Path(run["target_path"])
        if target.is_file():
            candidates.append(("revised_document", target, workspace))
        source_path = self._source_path(run)
        if source_path is not None and source_path.is_file():
            candidates.append(("source_snapshot", source_path, self.app_settings.projects_dir))
        ledger_path = workspace / "artifacts" / "audit.sqlite3"
        if ledger_path.is_file():
            candidates.append(("audit_ledger", ledger_path, workspace))
        for name, path_value in (result.artifacts or {}).items():
            path = Path(str(path_value))
            if path.is_file():
                candidates.append((name, path, workspace))

        for artifact_type, path, root in candidates:
            try:
                stored = assert_within(path, root)
                data = stored.read_bytes()
                self.artifacts.create(
                    run_id=run_id,
                    artifact_type=artifact_type,
                    filename=stored.name,
                    stored_path=str(stored),
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
            except (OSError, ValidationError) as exc:
                _log.warning(
                    f"could not index {artifact_type} artifact for run {run_id}: "
                    f"{type(exc).__name__}: {exc}"[:300],
                    extra={"role": "app", "run_id": run_id},
                )
    # -- reading -------------------------------------------------------------- #

    def summary(self, run: dict[str, Any]) -> WorkflowRunSummary:
        project = self.projects.get(str(run["project_id"]))
        source = self.sources.get(str(run["source_id"]))
        return WorkflowRunSummary(
            id=str(run["id"]),
            project_id=str(run["project_id"]),
            project_name=project["name"] if project else None,
            source_id=str(run["source_id"]),
            source_filename=source["filename"] if source else None,
            objective=str(run["objective"]),
            mode=str(run["mode"]),  # type: ignore[arg-type]
            status=str(run["status"]),  # type: ignore[arg-type]
            current_stage=run.get("current_stage"),
            created_at=str(run["created_at"]),
            started_at=run.get("started_at"),
            finished_at=run.get("finished_at"),
            revision_round=int(run.get("revision_round") or 0),
            failure_reason=run.get("failure_reason"),
            usage=run.get("usage") or {},
            config=run.get("config") or {},
            provider_snapshot=run.get("provider_snapshot") or {},
        )

    def stages(self, run_id: str) -> list[StageState]:
        """Derive the stage timeline from persisted events.

        Derived, not stored: the event log is the single record of what happened, so the
        timeline a user sees cannot disagree with the audit trail.
        """
        states = {stage: StageState(stage=stage, state="pending") for stage in STAGE_ORDER}
        run = self.runs.get(run_id)
        seen_revise = False
        for event in self.events.list_after(run_id, limit=5000):
            if event.type == "stage.started" and event.stage in states:
                states[event.stage].state = "running"
                states[event.stage].started_at = event.timestamp
            elif event.type == "stage.completed" and event.stage in states:
                outcome = str(event.payload.get("outcome") or "ok")
                outcome_states: dict[str, StageStateName] = {
                    "ok": "completed",
                    "degraded": "warning",
                    "warning": "warning",
                    "failed": "failed",
                    "invalid_output": "failed",
                    "retried": "warning",
                }
                states[event.stage].state = outcome_states.get(outcome, "completed")
                states[event.stage].finished_at = event.timestamp
                states[event.stage].detail = {
                    key: value
                    for key, value in event.payload.items()
                    if key in {"decision", "status", "reason", "round"} and value is not None
                }
            elif event.type == "stage.transition":
                target = str((event.payload.get("transition") or {}).get("to") or "")
                if target == "revise":
                    seen_revise = True
            elif event.type.startswith("workflow."):
                if event.type == "workflow.degraded":
                    states["verify"].state = "warning"
                elif event.type == "workflow.blocked" and "verify" in states:
                    states["verify"].state = "blocked"
                elif event.type == "workflow.failed" and "plan" in states and states["plan"].state == "pending":
                    states["plan"].state = "failed"

        timeline = [states[stage] for stage in STAGE_ORDER]
        if seen_revise:
            timeline.append(StageState(stage="revise", state="completed", detail={"rounds": "bounded revision loop"}))
        if run and run.get("status") in TERMINAL_STATUSES:
            # A terminal run cannot still have a stage in flight: the stage was interrupted
            # (cancellation, or a stage that never started because a gate stopped the run).
            # `verify` is the exception: it runs and legitimately ends unhappy.
            for entry in timeline:
                if entry.state == "running":
                    entry.state = "skipped" if entry.stage != "verify" else "warning"
        return timeline

    def inspect(self, run_id: str) -> RunInspection:
        """Everything needed to explain a run, assembled from engine-owned evidence."""
        run = self.require(run_id)
        outputs = self._stage_outputs(run)
        terminal = outputs.get("terminal")
        diff_evidence = outputs.get("diff")
        # The engine records the whole DiffEvidence (path, availability, hashes, byte counts)
        # because that is the evidence for the change. The UI renders the unified diff text,
        # so it is projected here rather than making the browser dig into the structure.
        diff_text = diff_evidence.get("unified_diff") if isinstance(diff_evidence, dict) else diff_evidence
        artifacts = [
            ArtifactRef(
                id=str(row["id"]),
                run_id=str(row["run_id"]),
                type=str(row["type"]),
                filename=str(row["filename"]),
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
                created_at=str(row["created_at"]),
                download_url=f"/api/v1/workflows/{run_id}/artifacts/{row['id']}/download",
            )
            for row in self.artifacts.list_for_run(run_id)
        ]
        summary = self.summary(run)
        return RunInspection(
            run=summary,
            stages=self.stages(run_id),
            plan=outputs.get("plan"),
            reviewers=list(outputs.get("reviewer_reports") or []),
            failed_reviewers=dict(outputs.get("failed_reviewers") or {}),
            synthesis=outputs.get("issue_map"),
            edit=outputs.get("edit_result"),
            diff=diff_text if isinstance(diff_text, str) else None,
            diff_review=outputs.get("diff_review"),
            adversarial=outputs.get("adversarial_verdict"),
            verification=outputs.get("verification"),
            final_report=outputs.get("final_report"),
            coverage=outputs.get("review_coverage"),
            unresolved_issues=list(outputs.get("unresolved_issues") or []),
            terminal=terminal,
            usage=summary.usage,
            artifacts=artifacts,
            status_explanation=self.explain(summary, outputs, artifacts),
        )

    def _stage_outputs(self, run: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(run["workspace_root"])
        engine_id = run.get("engine_workflow_id") or run["id"]
        path = workspace / "artifacts" / str(engine_id) / "stage_outputs.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def explain(self, summary: WorkflowRunSummary, outputs: dict[str, Any], artifacts: list[ArtifactRef]) -> str:
        """A sentence a user can act on, built only from recorded facts."""
        status = summary.status
        terminal = outputs.get("terminal") or {}
        rounds = int(terminal.get("rounds_executed") or summary.revision_round or 0)
        verification = (outputs.get("verification") or {}).get("results") or []
        failing = [item.get("check") for item in verification if not item.get("passed")]
        adversarial = outputs.get("adversarial_verdict") or {}
        failed_reviewers = outputs.get("failed_reviewers") or {}
        unresolved = outputs.get("unresolved_issues") or []

        if status == "succeeded":
            base = f"Every mandatory check passed after {rounds} revision round(s)."
        elif status == "degraded":
            base = "The gates passed, but reviewer coverage was incomplete, so the result is reported as degraded rather than successful."
        elif status == "blocked":
            base = (
                f"The workflow used all {rounds} revision round(s) and verification still refused to pass"
                + (f": {', '.join(str(item) for item in failing)}." if failing else ".")
            )
        elif status == "cancelled":
            base = "The run was cancelled; stages still in flight were stopped and nothing further was scheduled."
        elif status == "failed":
            base = f"The run failed before a result could be established. {summary.failure_reason or ''}".strip()
        elif status in {"running", "queued"}:
            base = f"The run is {status}; evidence appears as each stage reports."
        else:
            base = ""

        notes: list[str] = []
        if failed_reviewers:
            notes.append(
                "Missing reviewer coverage: "
                + "; ".join(f"{role} ({reason})" for role, reason in failed_reviewers.items())
            )
        if adversarial and adversarial.get("passed") is False:
            notes.append(
                f"The adversarial reviewer rejected the revision "
                f"({len(adversarial.get('critical_issues') or [])} critical, {len(adversarial.get('major_issues') or [])} major)."
            )
        if unresolved:
            notes.append(f"{len(unresolved)} issue(s) remain unresolved.")
        if not artifacts:
            notes.append("No artifacts were produced.")
        return " ".join([base, *notes]).strip()

    def effective_workflow_id(self, run_id: str) -> str:
        run = self.require(run_id)
        return str(run.get("engine_workflow_id") or run_id)

    def artifact_path(self, run_id: str, artifact_id: str) -> Path:
        """Resolve an artifact for download, refusing anything outside server-owned storage.

        A run's artifacts legitimately span two server-owned stores: the run workspace for
        what the engine produced, and the project's source store for the immutable snapshot
        the run ingested. Each kind is therefore validated against the root it belongs in,
        rather than against the run workspace alone - which rejected a legitimate source
        download - and rather than against nothing, which would make the containment check
        decorative.
        """
        row = self.artifacts.get(artifact_id)
        if row is None or str(row["run_id"]) != run_id:
            raise NotFoundError(f"artifact {artifact_id} does not belong to run {run_id}")
        run = self.require(run_id)
        path = Path(str(row["stored_path"]))
        if not path.is_file():
            raise NotFoundError(f"artifact {artifact_id} is missing from disk")
        artifact_type = str(row.get("artifact_type") or row.get("type") or "")
        if artifact_type == "source_snapshot":
            return assert_within(path, self.app_settings.projects_dir)
        # Re-checked at download time, not only at registration time.
        return assert_within(path, Path(run["workspace_root"]))

    def copy_workspace_file(self, run_id: str, relative: str, destination: Path) -> Path:
        """Copy one file out of a run workspace, refusing traversal."""
        run = self.require(run_id)
        workspace = Path(run["workspace_root"])
        source = assert_within(workspace / relative, workspace)
        if not source.is_file():
            raise NotFoundError(f"{relative} is not a file in run {run_id}")
        # Atomic for the same reason as source ingest: a partially copied target would be a
        # corrupt starting artifact for a run.
        atomic_write_bytes(destination, source.read_bytes())
        return destination

    def delete(self, run_id: str) -> None:
        raise ConflictError("runs are immutable history and cannot be deleted through the API")
