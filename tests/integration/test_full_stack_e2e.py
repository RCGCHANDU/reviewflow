"""Deterministic full-stack end-to-end test.

Walks the chain a user actually performs, through the real HTTP API, against the real
ReviewFlow engine (scripted model, so it is free and repeatable):

    create project -> upload source -> start workflow -> consume the live event stream
    -> reviewers report -> synthesis -> controlled edit -> diff review -> adversarial
    -> verification -> terminal state -> artifact download

It asserts that every stage left evidence behind, and that the served frontend is the
built application rather than a placeholder.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reviewflow_app.api.deps import AppState
from reviewflow_app.config import AppSettings
from reviewflow_app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def consume_stream(client: TestClient, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Read the run's SSE stream to completion and return (event type, payload) pairs."""
    response = client.get(f"/api/v1/workflows/{run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events: list[tuple[str, dict[str, Any]]] = []
    current_type = ""
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current_type = line.removeprefix("event: ")
        elif line.startswith("data: ") and current_type:
            events.append((current_type, json.loads(line.removeprefix("data: "))))
    return events


def test_full_stack_run_from_upload_to_artifact_download(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path / "app", static_dir=None)
    app = create_app(settings)

    with TestClient(app) as client:
        # 1. A project exists.
        project = client.post(
            "/api/v1/projects",
            json={"name": "Long-video paper", "description": "e2e", "default_objective": "find unsupported claims"},
        ).json()

        # 2. Source material is supplied (the deterministic sample the engine's scenarios use).
        source = client.post(
            f"/api/v1/projects/{project['id']}/samples",
            params={"sample_id": "scenario:successful_end_to_end"},
        ).json()
        assert source["filename"] == "manuscript.md"
        assert len(source["sha256"]) == 64

        # 3. Preflight passes before anything expensive happens.
        preflight = client.post(
            "/api/v1/workflows/preflight",
            json={
                "project_id": project["id"],
                "source_id": source["id"],
                "objective": "Identify unsupported claims and make only defensible edits.",
                "config": {"mode": "scripted"},
            },
        ).json()
        assert preflight["ok"] is True, preflight

        # 4. The workflow is created and handed to the executor.
        created = client.post(
            "/api/v1/workflows",
            json={
                "project_id": project["id"],
                "source_id": source["id"],
                "objective": "Identify unsupported claims and make only defensible edits.",
                "config": {"mode": "scripted", "scripted_scenario": "successful_end_to_end"},
            },
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        # 5. A page refresh must not lose the run: it is durable from the moment it is created.
        assert client.get(f"/api/v1/workflows/{run_id}").json()["run"]["id"] == run_id

        # 6. Live events stream to the client.
        deadline = time.time() + 60
        detail: dict[str, Any] = {}
        while time.time() < deadline:
            detail = client.get(f"/api/v1/workflows/{run_id}").json()
            if detail["run"]["status"] in {"succeeded", "blocked", "failed", "degraded", "cancelled"}:
                break
            time.sleep(0.05)

        events = consume_stream(client, run_id)
        types = [event_type for event_type, _ in events]
        assert types[0] == "workflow.created"
        assert "workflow.started" in types
        for expected in (
            "stage.started",
            "agent.started",
            "review.report_received",
            "synthesis.completed",
            "edit.completed",
            "diff_review.completed",
            "adversarial.completed",
            "verification.completed",
            "workflow.terminal",
            "workflow.succeeded",
            "stream.end",
        ):
            assert expected in types, f"missing {expected} in {types}"

        # The sequence numbers are contiguous from 1: nothing was dropped or reordered.
        payloads = [payload for _, payload in events if "seq" in payload]
        assert [payload["seq"] for payload in payloads] == list(range(1, len(payloads) + 1))

        # 7. Every stage left evidence behind.
        inspection = client.get(f"/api/v1/workflows/{run_id}/inspection").json()
        assert inspection["run"]["status"] == "succeeded"

        assert len(inspection["reviewers"]) == 4, "four independent reviewers must report"
        assert inspection["failed_reviewers"] == {}
        total_issues = sum(len(report["issues"]) for report in inspection["reviewers"])
        assert total_issues >= 1

        synthesis = inspection["synthesis"]
        assert synthesis["accepted_issues"], "the synthesis must accept the real defects"
        assert "change_required" in synthesis

        edit = inspection["edit"]
        assert edit["target"] == "manuscript.md"
        assert edit["changes"], "the controlled editor must report its changes"
        assert inspection["diff"].startswith("---"), "the diff is a real unified diff"

        assert inspection["adversarial"]["passed"] is True
        assert inspection["verification"]["results"], "verification results must be recorded"
        assert all("passed" in result for result in inspection["verification"]["results"])
        assert inspection["final_report"]["executive_summary"]
        assert inspection["coverage"]["succeeded"], "reviewer coverage must be recorded"

        # 8. The revised document is retrievable and actually changed.
        artifacts = client.get(f"/api/v1/workflows/{run_id}/artifacts").json()
        by_type = {artifact["type"]: artifact for artifact in artifacts}
        assert {"revised_document", "audit_events", "stage_outputs"} <= set(by_type)

        revised = client.get(f"/api/v1/workflows/{run_id}/artifacts/{by_type['revised_document']['id']}/download")
        assert revised.status_code == 200
        assert revised.content != b"", "the revised document must be downloadable"

        # The edit really landed, and it did the honest thing: the two claims the reviewers
        # flagged as unsupported were marked as such rather than left reading as established.
        # The source snapshot the run ingested is unchanged (checked separately below).
        assert b"preliminary and uncited" in revised.content
        assert b"not recorded in this draft" in revised.content
        original = client.get(f"/api/v1/workflows/{run_id}/artifacts/{by_type['source_snapshot']['id']}/download")
        assert b"preliminary and uncited" not in original.content
        assert len(revised.content) > len(original.content)


        # The staged evidence is downloadable too, and it is the engine's own contracts.
        outputs = client.get(f"/api/v1/workflows/{run_id}/artifacts/{by_type['stage_outputs']['id']}/download")
        staged = json.loads(outputs.content)
        assert {"plan", "reviewer_reports", "issue_map", "edit_result", "diff", "verification"} <= set(staged)
        assert staged["terminal"]["status"] == "succeeded"

        # 9. Usage is reported without inventing a price.
        usage = client.get(f"/api/v1/workflows/{run_id}").json()["run"]["usage"]
        assert usage["model_calls"] >= 1
        assert usage["total_tokens"] > 0
        assert usage["cost"]["amount"] is None and usage["cost"]["label"] == "unknown"

        # 10. The run is reopenable later, unchanged.
        reopened = client.get(f"/api/v1/workflows/{run_id}/inspection").json()
        assert reopened["run"] == inspection["run"]
        history = client.get("/api/v1/workflows").json()
        assert any(item["id"] == run_id for item in history)


@pytest.mark.skipif(
    not (REPO_ROOT / "frontend" / "dist" / "index.html").is_file(),
    reason="frontend has not been built (run `npm run build` in frontend/)",
)
def test_the_built_frontend_is_served_by_the_api(tmp_path: Path) -> None:
    """The production path: one process serves both the API and the built SPA."""
    dist = REPO_ROOT / "frontend" / "dist"
    settings = AppSettings(data_root=tmp_path / "app", static_dir=dist)
    app = create_app(settings)

    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        assert 'id="root"' in index.text

        # The SPA's asset bundle is served, and the API still answers on its own prefix.
        assets = [line for line in index.text.split('"') if line.startswith("/assets/")]
        assert assets, "the built index must reference its assets"
        asset = client.get(assets[0])
        assert asset.status_code == 200
        assert client.get("/api/v1/health").json()["status"] == "ok"

        # A deep link must serve the application shell, not the API's JSON 404: refreshing
        # on a run page is how a user reopens a review.
        for deep_link in ("/runs/run_abc123", "/providers", "/projects/prj_1", "/new"):
            response = client.get(deep_link)
            assert response.status_code == 200, deep_link
            assert "text/html" in response.headers["content-type"], deep_link
            assert 'id="root"' in response.text, deep_link

        # An unknown API path must still be a JSON 404 - the fallback must not swallow it,
        # or a mistyped endpoint would look like a success to anything checking the status.
        for missing_path in ("/api/v1/workflows/does_not_exist", "/api/v1/nope", "/api/v9/x"):
            missing = client.get(missing_path)
            assert missing.status_code == 404, missing_path
            assert missing.headers["content-type"].startswith("application/json"), missing_path
        assert client.get("/api/v1/health").status_code == 200


def test_a_run_interrupted_by_a_restart_is_not_reported_as_running(tmp_path: Path) -> None:
    """Durability check: state survives the process, and a dead run is not left 'running'.

    A second application instance over the same data root sees the first run's terminal
    state from the database - the stream can never be the only record.
    """
    data_root = tmp_path / "app"
    settings = AppSettings(data_root=data_root, static_dir=None)

    with TestClient(create_app(settings)) as first_client:
        project = first_client.post("/api/v1/projects", json={"name": "Durability"}).json()
        source = first_client.post(
            f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:successful_end_to_end"}
        ).json()
        run = first_client.post(
            "/api/v1/workflows",
            json={
                "project_id": project["id"],
                "source_id": source["id"],
                "objective": "durability",
                "config": {"mode": "scripted"},
            },
        ).json()
        deadline = time.time() + 60
        while time.time() < deadline:
            status = first_client.get(f"/api/v1/workflows/{run['id']}").json()["run"]["status"]
            if status in {"succeeded", "blocked", "failed", "degraded", "cancelled"}:
                break
            time.sleep(0.05)
        assert status == "succeeded"

    # A fresh process, same data root: the run and its evidence are still there.
    with TestClient(create_app(settings)) as second_client:
        detail = second_client.get(f"/api/v1/workflows/{run['id']}")
        assert detail.status_code == 200
        assert detail.json()["run"]["status"] == "succeeded"
        assert detail.json()["is_running"] is False
        inspection = second_client.get(f"/api/v1/workflows/{run['id']}/inspection").json()
        assert inspection["reviewers"], "the evidence must outlive the process that produced it"

        # Its event stream replays from the database even though nothing is executing.
        stream = second_client.get(f"/api/v1/workflows/{run['id']}/events").text
        assert "event: workflow.succeeded" in stream

    # And a run left non-terminal by a crash is reported as not executing, not as running.
    state = AppState.build(settings)
    crashed = state.runs.create(
        project_id=project["id"] if isinstance(project, dict) else "",
        source_id="src_x",
        objective="crashed",
        mode="scripted",
        config={},
        workspace_root=str(settings.run_workspace("pending")),
        target_path="",
    )
    state.runs.mark_started(str(crashed["id"]), engine_workflow_id="x", provider_snapshot={})
    with TestClient(create_app(settings)) as third_client:
        body = third_client.get(f"/api/v1/workflows/{crashed['id']}").json()
        assert body["is_running"] is False
        stream = third_client.get(f"/api/v1/workflows/{crashed['id']}/events").text
        assert "event: stream.end" in stream
        assert "no longer executing" in stream
