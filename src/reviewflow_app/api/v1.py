"""The API layer: versioned routes over the application services.

Handlers are thin on purpose - they validate transport concerns (status codes, request
bodies, streaming) and delegate to a service. No orchestration rule lives here, so the
same rules hold whether a request arrives from the browser, the E2E test or curl.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from reviewflow_app import schemas
from reviewflow_app.api.deps import AppState, get_state
from reviewflow_app.errors import AppError, InvalidConfigurationError, NotFoundError, ValidationError

router = APIRouter(prefix="/api/v1", tags=["reviewflow"])

#: Event types that end a stream: the run has reached a terminal state.
TERMINAL_EVENT_TYPES = {
    "workflow.succeeded",
    "workflow.blocked",
    "workflow.degraded",
    "workflow.failed",
    "workflow.cancelled",
}


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


@router.get("/health")
def health(state: AppState = Depends(get_state)) -> dict[str, Any]:
    """Liveness plus the operational facts a dashboard wants immediately."""
    provider = state.provider_service.status()
    return {
        "status": "ok",
        "data_root": str(state.settings.data_root),
        "active_provider": provider.active_provider,
        "providers_available": [item.name for item in provider.providers if item.available],
        "running_runs": state.executor.active(),
        "scripted_runs_enabled": state.settings.allow_scripted_runs,
        "live_runs_enabled": state.settings.allow_live_runs,
    }


# --------------------------------------------------------------------------- #
# providers, models, roles
# --------------------------------------------------------------------------- #


@router.get("/providers", response_model=schemas.ProvidersResponse)
def list_providers(
    provider: str | None = Query(None), state: AppState = Depends(get_state)
) -> schemas.ProvidersResponse:
    """Masked provider status. Never returns a token, key or credential-store body."""
    return state.provider_service.status(provider=provider)


@router.post("/providers/refresh", response_model=schemas.ProvidersResponse)
def refresh_providers(state: AppState = Depends(get_state)) -> schemas.ProvidersResponse:
    return state.provider_service.refresh()


@router.post("/providers/doctor", response_model=schemas.DoctorResponse)
async def run_provider_doctor(
    provider: str | None = Query(None),
    model: str | None = Query(None),
    state: AppState = Depends(get_state),
) -> schemas.DoctorResponse:
    """Live capability probe: makes real model calls. Explicit, never implicit."""
    return await state.provider_service.doctor(provider=provider, model=model)


@router.get("/models", response_model=list[schemas.ModelInfo])
def list_models(
    provider: str | None = Query(None),
    catalogue: bool = Query(False, description="also query each provider for its full model list"),
    state: AppState = Depends(get_state),
) -> list[schemas.ModelInfo]:
    return state.provider_service.models(provider=provider, catalogue=catalogue)


@router.get("/roles", response_model=list[schemas.RoleInfo])
def list_roles(provider: str | None = Query(None), state: AppState = Depends(get_state)) -> list[schemas.RoleInfo]:
    """Specialists the engine can run, with their permissions and default model."""
    return state.provider_service.roles(provider=provider)


# --------------------------------------------------------------------------- #
# projects and sources
# --------------------------------------------------------------------------- #


@router.get("/projects", response_model=list[schemas.Project])
def list_projects(state: AppState = Depends(get_state)) -> list[schemas.Project]:
    return [schemas.from_row(schemas.Project, row) for row in state.projects.list_all()]


@router.post("/projects", response_model=schemas.Project, status_code=201)
def create_project(payload: schemas.ProjectCreate, state: AppState = Depends(get_state)) -> schemas.Project:
    row = state.projects.create(
        name=payload.name, description=payload.description, default_objective=payload.default_objective
    )
    return schemas.from_row(schemas.Project, row)


@router.get("/projects/{project_id}")
def get_project(project_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    project = state.projects.get(project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} does not exist")
    sources = [
        schemas.from_row(
            schemas.SourceArtifact, row, preview=state.source_service.preview(str(row["id"]), limit=400)
        )
        for row in state.sources.list_for_project(project_id)
    ]
    runs = [state.run_service.summary(row) for row in state.runs.list_all(project_id=project_id, limit=20)]
    return {"project": schemas.from_row(schemas.Project, project), "sources": sources, "runs": runs}


@router.post("/projects/{project_id}/sources", response_model=schemas.SourceArtifact, status_code=201)
async def upload_source(
    project_id: str,
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
) -> schemas.SourceArtifact:
    """Accept one source file.

    The filename is sanitised and only ever decides the leaf name; the directory comes from
    server-generated ids, so nothing in this request can influence where bytes are written.
    """
    data = await file.read()
    row = state.source_service.upload(
        project_id=project_id,
        filename=file.filename or "source",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    return schemas.from_row(
        schemas.SourceArtifact, row, preview=state.source_service.preview(str(row["id"]), limit=400)
    )


@router.get("/projects/{project_id}/sources", response_model=list[schemas.SourceArtifact])
def list_sources(project_id: str, state: AppState = Depends(get_state)) -> list[schemas.SourceArtifact]:
    if state.projects.get(project_id) is None:
        raise NotFoundError(f"project {project_id} does not exist")
    return [
        schemas.from_row(
            schemas.SourceArtifact, row, preview=state.source_service.preview(str(row["id"]), limit=400)
        )
        for row in state.sources.list_for_project(project_id)
    ]


@router.get("/samples")
def list_samples(state: AppState = Depends(get_state)) -> list[dict[str, Any]]:
    """Deterministic sample material, so the app can be tried without bringing a document."""
    return [{key: value for key, value in sample.items() if key != "material"} for sample in state.source_service.samples()]


@router.post("/projects/{project_id}/samples", response_model=schemas.SourceArtifact, status_code=201)
def load_sample(
    project_id: str, sample_id: str = Query(...), state: AppState = Depends(get_state)
) -> schemas.SourceArtifact:
    row = state.source_service.upload_sample(project_id=project_id, sample_id=sample_id)
    return schemas.from_row(
        schemas.SourceArtifact, row, preview=state.source_service.preview(str(row["id"]), limit=400)
    )


# --------------------------------------------------------------------------- #
# workflows
# --------------------------------------------------------------------------- #


@router.get("/workflows")
def list_workflows(
    project_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    state: AppState = Depends(get_state),
) -> list[schemas.WorkflowRunSummary]:
    """Run history. Reading history never re-executes anything."""
    rows = state.runs.list_all(project_id=project_id, status=status, limit=limit)
    return [state.run_service.summary(row) for row in rows]


@router.post("/workflows/preflight", response_model=schemas.PreflightResult)
async def preflight(payload: schemas.PreflightRequest, state: AppState = Depends(get_state)) -> schemas.PreflightResult:
    """Check a run before it is created, so an impossible run is never started."""
    source_exists = False
    source_name = "(none)"
    if payload.source_id:
        row = state.sources.get(payload.source_id)
        if row is not None:
            source_exists = True
            source_name = str(row["filename"])
    return await state.provider_service.preflight(payload, source_exists=source_exists, source_name=source_name)


@router.post("/workflows", response_model=schemas.WorkflowRunSummary, status_code=201)
async def create_workflow(
    payload: schemas.RunCreateRequest, state: AppState = Depends(get_state)
) -> schemas.WorkflowRunSummary:
    """Create a run and hand it to the executor.

    The response returns as soon as the run is durable; progress arrives over the event
    stream, never by holding this request open.
    """
    # Authoritative gate: the backend rejects an unstartable configuration even if a client
    # skipped preflight. Same rule as the preflight endpoint, so the two cannot disagree.
    failures = state.provider_service.blocking_configuration_failures(payload.config)
    if failures:
        raise InvalidConfigurationError(
            " ".join(check.detail for check in failures) or "the requested configuration cannot be executed",
            detail={
                "checks": [
                    {"name": check.name, "code": check.code, "detail": check.detail} for check in failures
                ]
            },
        )

    run = state.run_service.create(payload)
    recorder = _run_recorder(state, str(run["id"]))
    recorder.emit(
        "workflow.created",
        payload={
            "mode": run["mode"],
            "objective": run["objective"],
            "config": run["config"],
        },
    )
    await state.executor.submit(str(run["id"]))
    return state.run_service.summary(state.run_service.require(str(run["id"])))


def _run_recorder(state: AppState, run_id: str) -> Any:
    from reviewflow_app.events import RunRecorder

    return RunRecorder.for_run(run_id, state.database, state.bus)


@router.get("/workflows/{run_id}")
def get_workflow(run_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    run = state.run_service.require(run_id)
    return {
        "run": state.run_service.summary(run),
        "stages": state.run_service.stages(run_id),
        "is_running": state.executor.is_running(run_id),
    }


@router.get("/workflows/{run_id}/inspection", response_model=schemas.RunInspection)
def inspect_workflow(run_id: str, state: AppState = Depends(get_state)) -> schemas.RunInspection:
    """The full evidence bundle: plan, reviewer reports, synthesis, edit, diff, verdict, checks."""
    return state.run_service.inspect(run_id)


@router.get("/workflows/{run_id}/issues")
def list_issues(run_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    """Findings, with reviewer independence preserved.

    Reviewer reports are returned per reviewer, exactly as the engine produced them, and
    reconciliation is reported separately. Flattening them into one list before synthesis
    would imply a consensus the run may not have reached.
    """
    inspection = state.run_service.inspect(run_id)
    dispositions: dict[str, dict[str, Any]] = {}
    synthesis = inspection.synthesis or {}
    for item in synthesis.get("accepted_issues") or []:
        dispositions[str(item.get("issue_id"))] = {"disposition": "accepted", **item}
    for item in synthesis.get("rejected_issues") or []:
        dispositions[str(item.get("issue_id"))] = {"disposition": "rejected", **item}
    for merged in synthesis.get("merged_issues") or []:
        for issue_id in merged.get("issue_ids") or []:
            dispositions[str(issue_id)] = {"disposition": "merged", "rationale": merged.get("rationale")}
    return {
        "reports": inspection.reviewers,
        "failed_reviewers": inspection.failed_reviewers,
        "conflicts": synthesis.get("conflicts") or [],
        "unresolved_questions": synthesis.get("unresolved_questions") or [],
        "dispositions": dispositions,
        "unresolved_issues": inspection.unresolved_issues,
        "total_issues": sum(len(report.get("issues") or []) for report in inspection.reviewers),
    }


@router.post("/workflows/{run_id}/cancel")
async def cancel_workflow(run_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    """Stop a running workflow.

    Cancellation is recorded as its own terminal state, never as a failure. A file mutation
    already in its atomic commit is allowed to finish: interrupting it would leave a
    partially written target, which is exactly what the engine's write path prevents.
    """
    run = state.run_service.require(run_id)
    if not state.executor.is_running(run_id):
        raise ValidationError(
            f"run {run_id} is not executing (status: {run['status']}); only a running workflow can be cancelled"
        )
    await state.executor.cancel(run_id)
    return {"run_id": run_id, "cancelled": True}


@router.get("/workflows/{run_id}/artifacts", response_model=list[schemas.ArtifactRef])
def list_run_artifacts(run_id: str, state: AppState = Depends(get_state)) -> list[schemas.ArtifactRef]:
    return state.run_service.inspect(run_id).artifacts


@router.get("/workflows/{run_id}/download/diff", response_class=PlainTextResponse)
def download_diff(run_id: str, state: AppState = Depends(get_state)) -> PlainTextResponse:
    """The run's unified diff as a patch file.

    Served as text rather than kept as a third stored artifact: it is derived from the two
    documents the run already keeps, and storing a copy would create something that can
    drift from the evidence it is supposed to represent.
    """
    run = state.run_service.require(run_id)
    inspection = state.run_service.inspect(run_id)
    if not inspection.diff:
        raise NotFoundError(
            f"run {run_id} has no diff: the controlled editor made no change"
            if run["status"] in {"succeeded", "blocked", "degraded"}
            else f"run {run_id} has not reached the controlled edit stage"
        )
    return PlainTextResponse(
        inspection.diff,
        media_type="text/x-patch",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.patch"'},
    )


@router.get("/workflows/{run_id}/artifacts/{artifact_id}/download")
def download_artifact(run_id: str, artifact_id: str, state: AppState = Depends(get_state)) -> FileResponse:
    """Download one artifact. The path is re-validated against the run workspace here."""
    path = state.run_service.artifact_path(run_id, artifact_id)
    row = state.artifacts.get(artifact_id)
    filename = str(row["filename"]) if row else path.name
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.post("/workflows/{run_id}/rerun", response_model=schemas.WorkflowRunSummary, status_code=201)
async def rerun_workflow(
    run_id: str,
    payload: schemas.RunConfigSnapshot | None = None,
    state: AppState = Depends(get_state),
) -> schemas.WorkflowRunSummary:
    """Start a *new* run from an old one, optionally with a changed configuration.

    The source is reused by reference (it is immutable); the previous run is left exactly
    as it was. Nothing about history is mutated by re-running.
    """
    previous = state.run_service.require(run_id)
    config = payload or schemas.RunConfigSnapshot.model_validate(previous["config"])
    request = schemas.RunCreateRequest(
        project_id=str(previous["project_id"]),
        source_id=str(previous["source_id"]),
        objective=str(previous["objective"]),
        config=config,
    )
    return await create_workflow(request, state)


@router.get("/workflows/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after: int | None = Query(None, description="replay events after this sequence number"),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    """Server-Sent Events for one run.

    Replay-then-live: persisted events are sent first (so a refresh or a reconnect loses
    nothing), then the live queue is drained. ``Last-Event-ID`` is honoured, which is what
    makes the browser's own reconnection logic correct rather than approximate.
    """
    run = state.run_service.require(run_id)
    header = request.headers.get("last-event-id")
    cursor = after if after is not None else (int(header) if header and header.isdigit() else 0)

    async def event_source() -> Any:
        nonlocal cursor
        for event in state.events.list_after(run_id, after_seq=cursor, limit=5000):
            cursor = event.seq
            yield _sse(event.seq, event.type, event.model_dump(mode="json"))
        if str(run["status"]) in schemas.TERMINAL_STATUSES:
            yield _sse(cursor, "stream.end", {"status": run["status"], "reason": "run already finished"})
            return
        if not state.executor.is_running(run_id):
            # Not terminal and not executing: the run was interrupted (a restart, a killed
            # process). Ending the stream is the truthful outcome - an endless keep-alive
            # would just hide it from the client.
            yield _sse(
                cursor,
                "stream.end",
                {
                    "status": run["status"],
                    "reason": "the run is no longer executing in this process; its last persisted state is shown",
                },
            )
            return

        async with state.bus.subscribe(run_id) as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    # The executor closed the stream when the run terminated; anything the
                    # subscriber missed is replayed from the database before ending.
                    for missed in state.events.list_after(run_id, after_seq=cursor, limit=5000):
                        cursor = missed.seq
                        yield _sse(missed.seq, missed.type, missed.model_dump(mode="json"))
                    yield _sse(cursor, "stream.end", {"reason": "run finished"})
                    return
                if item.seq <= cursor:
                    continue
                cursor = item.seq
                yield _sse(item.seq, item.type, item.model_dump(mode="json"))
                if item.type in TERMINAL_EVENT_TYPES:
                    yield _sse(cursor, "stream.end", {"status": item.payload.get("status")})
                    return

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _sse(event_id: int, event_type: str, payload: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(payload, default=str)}\n\n"


__all__ = ["AppError", "HTTPException", "Path", "router"]
