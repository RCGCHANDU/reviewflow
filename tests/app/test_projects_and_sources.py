"""Projects, source uploads and file safety."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from reviewflow_app.security.paths import sanitize_filename


def test_create_list_and_fetch_a_project(client: TestClient) -> None:
    created = client.post(
        "/api/v1/projects", json={"name": "Long-video paper", "description": "review", "default_objective": "check claims"}
    )
    assert created.status_code == 201
    project = created.json()
    assert project["id"].startswith("prj_")
    assert project["default_objective"] == "check claims"

    listed = client.get("/api/v1/projects").json()
    assert [item["id"] for item in listed] == [project["id"]]

    detail = client.get(f"/api/v1/projects/{project['id']}").json()
    assert detail["project"]["name"] == "Long-video paper"
    assert detail["sources"] == []
    assert detail["runs"] == []


def test_project_name_must_not_be_blank(client: TestClient) -> None:
    response = client.post("/api/v1/projects", json={"name": "   "})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_failed"


def test_unknown_project_is_a_structured_404(client: TestClient) -> None:
    response = client.get("/api/v1/projects/prj_missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "does not exist" in body["error"]["message"]
    # Errors carry a retryable flag so a client can decide whether to try again.
    assert body["error"]["retryable"] is False


def test_upload_stores_bytes_unchanged_and_records_a_hash(tmp_path: Path, client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    content = b"# Title\n\nA claim that needs evidence.\n"
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("manuscript.md", io.BytesIO(content), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    source = response.json()
    assert source["size_bytes"] == len(content)
    assert source["filename"] == "manuscript.md"
    assert source["sha256"] == __import__("hashlib").sha256(content).hexdigest()
    assert source["preview"].startswith("# Title")

    # The file on disk is byte-identical to what was uploaded: it is the immutable source.
    stored = list((Path(client.app.state.app_state.settings.data_root) / "projects").rglob("manuscript.md"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == content


def test_a_traversing_filename_cannot_escape_the_project_directory(client: TestClient, app_settings: Any) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("../../../evil.md", io.BytesIO(b"x"), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    # The name is reduced to its last component; nothing is written outside the root.
    assert response.json()["filename"] == "evil.md"
    assert not (app_settings.data_root.parent / "evil.md").exists()
    for path in Path(app_settings.data_root).rglob("evil.md"):
        assert app_settings.projects_dir in path.parents


def test_oversized_upload_is_rejected(client: TestClient, app_settings: Any) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    oversized = b"a" * (app_settings.max_upload_bytes + 1)
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("big.md", io.BytesIO(oversized), "text/markdown")},
    )
    assert response.status_code == 422
    assert "over the" in response.json()["error"]["message"]


def test_a_disallowed_extension_is_rejected(client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert response.status_code == 422
    assert "not an accepted source type" in response.json()["error"]["message"]


def test_an_empty_upload_is_rejected(client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("empty.md", io.BytesIO(b""), "text/markdown")},
    )
    assert response.status_code == 422
    assert "empty" in response.json()["error"]["message"]


def test_samples_can_be_listed_and_loaded(client: TestClient) -> None:
    samples = client.get("/api/v1/samples").json()
    assert any(sample["id"] == "scenario:successful_end_to_end" for sample in samples)
    # Sample listings describe the material without inlining it.
    assert all("material" not in sample for sample in samples)

    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    loaded = client.post(
        f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:successful_end_to_end"}
    )
    assert loaded.status_code == 201
    # Named manuscript.md on purpose: the scripted scenarios are authored against that name.
    assert loaded.json()["filename"] == "manuscript.md"

    missing = client.post(f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "nope"})
    assert missing.status_code == 404


def test_sanitize_filename_is_defensive() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Windows\\win.ini") == "win.ini"
    assert sanitize_filename("") == "source"
    assert sanitize_filename("...") == "source"
    assert sanitize_filename("nul") == "_nul"
    assert len(sanitize_filename("x" * 400 + ".md")) <= 120
    # No separators can survive, whatever the input.
    for hostile in ("a/b/c.md", "..\\..\\x.md", "a\\b.md", "\x00bad.md"):
        cleaned = sanitize_filename(hostile)
        assert "/" not in cleaned and "\\" not in cleaned


def test_source_preview_never_returns_a_binary_blob(client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "P"}).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        files={"file": ("data.json", io.BytesIO(json.dumps({"a": 1}).encode()), "application/json")},
    )
    assert response.status_code == 201
    assert "a" in (response.json()["preview"] or "")
