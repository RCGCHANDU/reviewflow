"""Workflow runs through the API: creation, live events, evidence, artifacts, cancellation."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reviewflow_app.events import EventBus, RunRecorder
from reviewflow_app.persistence.db import Database
from reviewflow_app.schemas import RunEvent

from .conftest import start_run, wait_for_terminal


def test_a_scripted_run_completes_and_exposes_its_evidence(client: TestClient, project: dict[str, Any], source: dict[str, Any]) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    assert run["status"] in {"queued", "running"}
    assert run["config"]["mode"] == "scripted"

    detail = wait_for_terminal(client, run["id"])
    assert detail["run"]["status"] == "succeeded"

    stages = {stage["stage"]: stage["state"] for stage in detail["stages"]}
    assert stages["ingest"] == "completed"
    assert stages["parallel_review"] == "completed"
    assert stages["controlled_edit"] == "completed"
    assert stages["verify"] == "completed"

    inspection = client.get(f"/api/v1/workflows/{run['id']}/inspection").json()
    assert [report["reviewer"] for report in inspection["reviewers"]] == [
        "evidence_auditor",
        "technical_reviewer",
        "rhetorical_analyst",
        "structure_reviewer",
    ]
    assert sum(len(report["issues"]) for report in inspection["reviewers"]) > 0
    assert inspection["synthesis"]["accepted_issues"], "the synthesis must accept the real defects"
    assert inspection["diff"] and "+" in inspection["diff"]
    assert inspection["verification"]["results"], "verification results must be recorded"
    assert inspection["run"]["status"] == "succeeded"
    assert "passed" in inspection["status_explanation"]
    assert inspection["artifacts"], "a finished run lists its artifacts"


def test_issues_endpoint_preserves_reviewer_independence(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    body = client.get(f"/api/v1/workflows/{run['id']}/issues").json()
    # Reports stay per reviewer; reconciliation is reported separately rather than merged in.
    assert len(body["reports"]) == 4
    assert all("reviewer" in report for report in body["reports"])
    assert body["total_issues"] >= 1
    assert isinstance(body["dispositions"], dict)
    assert body["dispositions"], "accepted issues must carry a disposition"
    for issue_id, decision in body["dispositions"].items():
        assert decision["disposition"] in {"accepted", "rejected", "merged"}
        assert issue_id


def test_events_are_persisted_in_order_and_replay_identically(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    stream = client.get(f"/api/v1/workflows/{run['id']}/events").text
    assert "event: workflow.started" in stream
    assert "event: stage.started" in stream
    assert "event: workflow.succeeded" in stream
    assert "event: stream.end" in stream

    seqs = [int(line.split(": ")[1]) for line in stream.splitlines() if line.startswith("id: ")]
    assert seqs == sorted(seqs), "events must be replayed in sequence order"

    # Replaying from a sequence number returns only what follows, which is what makes a
    # reconnecting client lose nothing and duplicate nothing.
    first_event_id = seqs[0]
    replay = client.get(f"/api/v1/workflows/{run['id']}/events?after={first_event_id}").text
    replayed_ids = [int(line.split(": ")[1]) for line in replay.splitlines() if line.startswith("id: ")]
    assert replayed_ids
    assert min(replayed_ids) > first_event_id


def test_artifact_download_returns_the_revised_document(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    artifacts = client.get(f"/api/v1/workflows/{run['id']}/artifacts").json()
    kinds = {artifact["type"] for artifact in artifacts}
    assert {"revised_document", "audit_events", "stage_outputs"} <= kinds

    document = next(artifact for artifact in artifacts if artifact["type"] == "revised_document")
    response = client.get(f"/api/v1/workflows/{run['id']}/artifacts/{document['id']}/download")
    assert response.status_code == 200
    # The edited document differs from the uploaded source: the run really changed it.
    assert response.content != client.get(f"/api/v1/projects/{project['id']}/sources").json()[0].get("preview", "").encode()

    # An artifact from another run is not reachable through this run's id.
    other = start_run(client, project_id=project["id"], source_id=source["id"])
    mismatched = client.get(f"/api/v1/workflows/{other['id']}/artifacts/{document['id']}/download")
    assert mismatched.status_code == 404


def test_creating_a_run_for_a_foreign_source_is_refused(client: TestClient, project: dict[str, Any], source: dict[str, Any]) -> None:
    other = client.post("/api/v1/projects", json={"name": "Other"}).json()
    response = client.post(
        "/api/v1/workflows",
        json={
            "project_id": other["id"],
            "source_id": source["id"],
            "objective": "review",
            "config": {"mode": "scripted"},
        },
    )
    assert response.status_code == 404
    assert "does not belong" in response.json()["error"]["message"]


def test_an_unknown_scripted_scenario_is_rejected_at_creation(client: TestClient, project: dict[str, Any], source: dict[str, Any]) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={
            "project_id": project["id"],
            "source_id": source["id"],
            "objective": "review",
            "config": {"mode": "scripted", "scripted_scenario": "does_not_exist"},
        },
    )
    assert response.status_code == 201
    # The run exists but fails honestly rather than silently doing nothing.
    run = wait_for_terminal(client, response.json()["id"])
    assert run["run"]["status"] == "failed"
    assert "unknown scripted scenario" in (run["run"]["failure_reason"] or "")


def test_a_live_run_without_a_credential_is_refused_before_it_is_created(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """A run that cannot execute must not exist at all.

    This used to create the run and let it fail at execution, which spent a workspace, a
    queue slot and the user's attention on a run whose outcome was already known. The
    invariant the old test protected - no model call is attempted without a provider - is
    now enforced earlier and more strictly: there is no run to call a model from.
    """
    run = start_run(client, project_id=project["id"], source_id=source["id"], provider="commandcode")
    response = client.post(
        "/api/v1/workflows",
        json={
            "project_id": project["id"],
            "source_id": source["id"],
            "objective": "review",
            "config": {"mode": "live", "provider": "commandcode"},
        },
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_configuration"
    codes = {check.get("code") for check in body["error"]["detail"]["checks"]}
    assert "PROVIDER_UNAVAILABLE" in codes, codes

    runs = client.get("/api/v1/workflows").json()
    runs = runs if isinstance(runs, list) else runs.get("runs", [])
    assert all(item["id"] != response.headers.get("x-run-id") for item in runs)
    assert run["id"] in {item["id"] for item in runs}, "the pre-existing scripted run is untouched"


def test_cancelling_a_terminal_run_is_refused_with_a_reason(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    response = client.post(f"/api/v1/workflows/{run['id']}/cancel")
    assert response.status_code == 422
    assert "only a running workflow can be cancelled" in response.json()["error"]["message"]


def test_cancelling_a_running_workflow_records_cancelled_not_failed(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """A run the user stops is cancelled - a distinct terminal state, never a failure."""
    # This scenario deliberately pauses in a reviewer, which gives a real window to cancel.
    run = start_run(
        client,
        project_id=project["id"],
        source_id=source["id"],
        scenario="reviewer_timeout_degrades",
    )

    deadline = time.time() + 20
    started = False
    while time.time() < deadline and not started:
        detail = client.get(f"/api/v1/workflows/{run['id']}").json()
        started = detail["run"]["status"] == "running" and detail["run"]["current_stage"] == "parallel_review"
        time.sleep(0.05)
    assert started, "the run never reported itself running in the parallel review stage"

    cancel = client.post(f"/api/v1/workflows/{run['id']}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    detail = wait_for_terminal(client, run["id"], timeout=30)
    assert detail["run"]["status"] == "cancelled"
    assert "cancelled" in (detail["run"]["failure_reason"] or "").lower()
    # Cancellation is recorded in the ledger like any other transition.
    events = client.get(f"/api/v1/workflows/{run['id']}/events").text
    assert "event: workflow.cancelled" in events


def test_run_history_is_read_only(client: TestClient, project: dict[str, Any], source: dict[str, Any]) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    before = client.get(f"/api/v1/workflows/{run['id']}").json()
    listed = client.get("/api/v1/workflows").json()
    assert any(item["id"] == run["id"] for item in listed)
    filtered = client.get("/api/v1/workflows", params={"status": "succeeded"}).json()
    assert all(item["status"] == "succeeded" for item in filtered)
    by_project = client.get("/api/v1/workflows", params={"project_id": project["id"]}).json()
    assert {item["id"] for item in after_runs(by_project)} == {run["id"]}

    after = client.get(f"/api/v1/workflows/{run['id']}").json()
    assert after["run"] == before["run"], "reading history must not change the run"


def after_runs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return items


def test_rerun_creates_a_new_run_and_leaves_the_original_untouched(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    original = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, original["id"])
    snapshot = client.get(f"/api/v1/workflows/{original['id']}").json()

    rerun = client.post(f"/api/v1/workflows/{original['id']}/rerun")
    assert rerun.status_code == 201
    assert rerun.json()["id"] != original["id"]
    assert rerun.json()["source_id"] == original["source_id"]

    wait_for_terminal(client, rerun.json()["id"])
    assert client.get(f"/api/v1/workflows/{original['id']}").json() == snapshot


def test_run_events_are_persisted_independently_of_any_client(state: Any) -> None:
    """The event log is durable, so a page refresh cannot erase a run's history."""
    database: Database = state.database
    project = state.projects.create(name="Events", description="", default_objective="")
    run = state.runs.create(
        project_id=str(project["id"]),
        source_id="src_none",
        objective="event durability",
        mode="scripted",
        config={},
        workspace_root=str(state.settings.run_workspace("pending")),
        target_path="",
    )
    database = state.database
    run_id = str(run["id"])
    recorder = RunRecorder.for_run(run_id, database, EventBus())
    recorder.emit("workflow.created", payload={"mode": "scripted"})
    recorder.emit("stage.started", stage="ingest", payload={"round": 0})

    # A brand new recorder for the same run continues the sequence rather than restarting it.
    resumed = RunRecorder.for_run(run_id, database, EventBus())
    third = resumed.emit("stage.completed", stage="ingest", payload={"outcome": "ok"})

    assert [first.seq for first in state.events.list_after(run_id)] == [1, 2, 3]
    assert third.seq == 3
    persisted = state.events.list_after(run_id)
    assert all(isinstance(event, RunEvent) for event in persisted)
    assert json.loads(json.dumps(persisted[0].model_dump(mode="json")))["payload"]["mode"] == "scripted"


def test_artifact_path_traversal_is_refused(state: Any) -> None:
    from reviewflow_app.errors import NotFoundError

    try:
        state.run_service.artifact_path("run_missing", "art_missing")
    except NotFoundError as error:
        assert "does not belong" in str(error) or "does not exist" in str(error)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("an unknown artifact must not resolve to a path")


def test_workflow_workspace_is_derived_from_the_run_id(
    client: TestClient, project: dict[str, Any], source: dict[str, Any], app_settings: Any
) -> None:
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    detail = wait_for_terminal(client, run["id"])
    assert detail["run"]["status"] == "succeeded"

    run_row = client.app.state.app_state.runs.get(run["id"])  # type: ignore[attr-defined]
    workspace = Path(run_row["workspace_root"]).resolve()
    assert workspace == (app_settings.runs_dir / run["id"] / "workspace").resolve()
    # The engine's target lives inside that workspace, and the uploaded source is untouched.
    target = Path(run_row["target_path"]).resolve()
    assert workspace in target.parents


def test_every_emitted_event_type_is_in_the_declared_vocabulary(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """The event stream must speak one vocabulary.

    Regression test for a real defect: the engine's event sink reported ``agent_started``
    while the ledger reported ``agent.completed``, so the UI's live reviewer panel - which
    listens for ``agent.started`` - never received a start signal in the running
    application, even though component tests passed against mocked events.
    """
    from reviewflow_app.events import EVENT_TYPES

    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    stream = client.get(f"/api/v1/workflows/{run['id']}/events").text
    emitted = {line.removeprefix("event: ") for line in stream.splitlines() if line.startswith("event: ")}

    undeclared = emitted - EVENT_TYPES
    assert not undeclared, f"events emitted outside the declared vocabulary: {sorted(undeclared)}"
    # Specifically: the dotted form the UI listens for, not the engine's underscore form.
    assert "agent.started" in emitted
    assert "agent_started" not in emitted
    # And no fact is reported twice under two names.
    assert "agent_completed" not in emitted
    assert "agent.completed" in emitted


def test_artifact_bookkeeping_failure_does_not_rewrite_the_verified_verdict(
    client: TestClient, project: dict[str, Any], source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified run must not be reported as failed because indexing an artifact broke.

    Regression test: the artifact indexer originally used the run workspace as the only
    allowed root, so registering the source snapshot (which correctly lives in the
    project's source store) raised - and because the failure escaped, a run that had passed
    every verification check was recorded as `failed`. Losing the pointer to evidence is a
    bookkeeping problem; it is not evidence that the work was wrong.
    """
    state = client.app.state.app_state  # type: ignore[attr-defined]

    def boom(**_: Any) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(state.artifacts, "create", boom)

    run = start_run(client, project_id=project["id"], source_id=source["id"])
    final = wait_for_terminal(client, run["id"])

    assert final["run"]["status"] == "succeeded", final
    # The documents are still on disk even though they could not be indexed.
    assert Path(state.run_service.require(run["id"])["target_path"]).is_file()
    assert client.get(f"/api/v1/workflows/{run['id']}/artifacts").json() == []


def test_every_registered_artifact_downloads_and_matches_its_hash(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """Every artifact the index advertises must actually be downloadable, byte for byte.

    Regression test: the source snapshot is stored in the project's source store rather than
    the run workspace, and download validation only allowed the workspace - so the snapshot
    appeared in the artifact list and then answered 400. An index that offers something it
    cannot serve is worse than an index that omits it.
    """
    run = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, run["id"])

    artifacts = client.get(f"/api/v1/workflows/{run['id']}/artifacts").json()
    types = {artifact["type"] for artifact in artifacts}
    assert {"revised_document", "source_snapshot", "stage_outputs", "audit_events"} <= types

    for artifact in artifacts:
        response = client.get(f"/api/v1/workflows/{run['id']}/artifacts/{artifact['id']}/download")
        assert response.status_code == 200, f"{artifact['type']}: {response.text[:200]}"
        assert len(response.content) == artifact["size_bytes"], artifact["type"]
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"], artifact["type"]

    # The diff is served as a patch, and it is the unified diff the engine computed.
    patch = client.get(f"/api/v1/workflows/{run['id']}/download/diff")
    assert patch.status_code == 200
    assert patch.text.startswith("--- ")


def test_an_artifact_from_another_run_is_not_reachable(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """An artifact id must only be servable through the run that produced it."""
    first = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, first["id"])
    second = start_run(client, project_id=project["id"], source_id=source["id"])
    wait_for_terminal(client, second["id"])

    artifact = client.get(f"/api/v1/workflows/{first['id']}/artifacts").json()[0]
    crossed = client.get(f"/api/v1/workflows/{second['id']}/artifacts/{artifact['id']}/download")
    assert crossed.status_code == 404
