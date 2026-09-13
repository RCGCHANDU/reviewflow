"""Fixtures for application tests.

Every test gets its own data root under a temporary directory, so the application's
database, sources, run workspaces and artifacts never touch the developer's real store —
and, critically, never touch the real credential stores: provider discovery is exercised
through isolated paths, and one test asserts that the real Codex store is untouched.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reviewflow_app.api.deps import AppState
from reviewflow_app.config import AppSettings
from reviewflow_app.main import create_app

TERMINAL = {"succeeded", "degraded", "blocked", "failed", "cancelled"}


@pytest.fixture
def app_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    """Application settings isolated to a temporary root."""
    # Point credential discovery at paths that do not exist, so a provider never becomes
    # available by accident and no test can reach a real credential store.
    monkeypatch.setenv("REVIEWFLOW_CODEX_AUTH_PATH", str(tmp_path / "codex" / "auth.json"))
    monkeypatch.setenv("REVIEWFLOW_COMMANDCODE_AUTH_PATH", str(tmp_path / "commandcode" / "auth.json"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for variable in ("OPENAI_API_KEY", "REVIEWFLOW_OPENAI_API_KEY", "COMMANDCODE_API_KEY", "REVIEWFLOW_PROVIDER"):
        monkeypatch.delenv(variable, raising=False)
    return AppSettings(data_root=tmp_path / "app", static_dir=None)


@pytest.fixture
def state(app_settings: AppSettings) -> AppState:
    """Application state without the HTTP layer (for service-level tests)."""
    return AppState.build(app_settings)


@pytest.fixture
def client(app_settings: AppSettings) -> Iterator[TestClient]:
    """A client bound to the real application object and its lifespan."""
    app = create_app(app_settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/projects", json={"name": "Paper review", "description": "test project"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def source(client: TestClient, project: dict[str, Any]) -> dict[str, Any]:
    """The deterministic sample, which the scripted scenarios are authored against."""
    response = client.post(
        f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:successful_end_to_end"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_run(
    client: TestClient,
    *,
    project_id: str,
    source_id: str,
    objective: str = "Identify unsupported claims and make only defensible edits.",
    scenario: str = "successful_end_to_end",
    rounds: int = 2,
    provider: str = "auto",
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/workflows",
        json={
            "project_id": project_id,
            "source_id": source_id,
            "objective": objective,
            "config": {
                "mode": "scripted",
                "provider": provider,
                "scripted_scenario": scenario,
                "limits": {"max_revision_rounds": rounds},
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_terminal(client: TestClient, run_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    """Poll the run until it reaches a terminal state, or fail loudly."""
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/workflows/{run_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["run"]["status"] in TERMINAL:
            return last
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach a terminal state within {timeout}s (last: {last.get('run')})")
