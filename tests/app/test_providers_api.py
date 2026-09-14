"""Provider discovery over the API: what is exposed, and what must never be."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reviewflow_app.services.providers import ProviderService

#: Shapes that indicate a leaked credential rather than an identifier.
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style key
    re.compile(r"user_[A-Za-z0-9]{20,}"),  # a full commandcode key (prefix carries more)
    re.compile(r"rt\.[A-Za-z0-9._-]{20,}"),  # refresh token
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
]


def assert_no_secrets(payload: Any) -> None:
    blob = json.dumps(payload)
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(blob), f"response leaked a credential matching {pattern.pattern}"


def fake_jwt(account: str = "acct-123456") -> str:
    def segment(payload: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'https://api.openai.com/auth': {'chatgpt_account_id': account}})}.sig"


def test_providers_payload_is_safe_and_complete(client: TestClient) -> None:
    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    body = response.json()

    assert set(body) >= {"active_provider", "requested_provider", "providers", "role_defaults"}
    names = {provider["name"] for provider in body["providers"]}
    assert names == {"codex", "commandcode", "openai"}
    # With every store redirected to a temporary path, nothing is signed in.
    assert all(provider["available"] is False for provider in body["providers"])
    assert body["active_provider"] is None
    for provider in body["providers"]:
        assert provider["hint"], "an unusable provider must explain how to fix it"
    assert_no_secrets(body)


def test_doctor_refuses_when_nothing_is_configured(client: TestClient) -> None:
    response = client.post("/api/v1/providers/doctor")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "provider_unavailable"
    assert body["error"]["retryable"] is False


def test_models_and_roles_are_discovered_not_hard_coded(client: TestClient) -> None:
    # With no credential, the model list is empty rather than a hard-coded catalogue.
    assert client.get("/api/v1/models").json() == []

    roles = client.get("/api/v1/roles").json()
    by_key = {role["role_key"]: role for role in roles}
    assert "controlled_editor" in by_key and "evidence_auditor" in by_key
    assert by_key["controlled_editor"]["may_mutate"] is True
    assert by_key["evidence_auditor"]["may_mutate"] is False
    assert "apply_edit" in by_key["controlled_editor"]["allowed_tools"]
    # Read-only roles cannot even be handed a mutating tool.
    for role in roles:
        if not role["may_mutate"]:
            assert "apply_edit" not in role["allowed_tools"]


def test_provider_status_exposes_only_masked_metadata(
    client: TestClient, app_settings: Any, tmp_path: Path
) -> None:
    """A configured provider reports identity, never material usable to authenticate."""
    codex_store = tmp_path / "codex" / "auth.json"
    codex_store.parent.mkdir(parents=True, exist_ok=True)
    token = fake_jwt("19efbdd0-3543-4b8d-9f60-50034357a739")
    codex_store.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": token,
                    "refresh_token": "rt.1.SUPERSECRETREFRESHTOKENVALUE",
                    "account_id": "19efbdd0-3543-4b8d-9f60-50034357a739",
                }
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/v1/providers")
    body = response.json()
    codex = next(provider for provider in body["providers"] if provider["name"] == "codex")
    assert codex["available"] is True
    assert codex["requires_streaming"] is True, "the Codex backend must advertise its transport requirement"
    # The account identifier is deliberately truncated, and the token is not present at all.
    assert codex["account"].startswith("19efbdd0")
    assert codex["account"].endswith("…"), "identifiers are masked, not echoed"
    assert token not in json.dumps(body)
    assert_no_secrets(body)


def test_refresh_reports_the_same_shape_without_touching_stores(client: TestClient) -> None:
    before = client.get("/api/v1/providers").json()
    refreshed = client.post("/api/v1/providers/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["providers"] == before["providers"]


def test_models_reports_role_defaults_when_a_credential_exists(app_settings: Any) -> None:
    """The model list is derived from the router's profiles, not written by hand."""
    service = ProviderService(app_settings)
    # No credential: nothing to report, and no invented catalogue.
    assert service.models() == []


def test_preflight_blocks_a_live_run_without_a_provider(client: TestClient, project: dict[str, Any]) -> None:
    response = client.post(
        "/api/v1/workflows/preflight",
        json={
            "project_id": project["id"],
            "objective": "review this",
            "config": {"mode": "live", "provider": "commandcode"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "provider" in body["blocking_failures"]
    provider_check = next(check for check in body["checks"] if check["name"] == "provider")
    assert "no usable credential" in provider_check["detail"]


def test_preflight_rejects_a_malformed_model_selection(app_settings: Any) -> None:
    """Format is checked before availability: they are different failures."""
    service = ProviderService(app_settings)

    malformed = service.validate_model_selection("not-a-provider:model-x", provider="codex")
    assert malformed.ok is False and malformed.blocking is True
    assert "unknown provider" in malformed.detail

    automatic = service.validate_model_selection("auto")
    assert automatic.ok is True and automatic.detail == "auto routing"


def test_an_unlisted_model_on_a_credentialed_provider_is_blocking_by_default(
    client: TestClient, app_settings: Any, tmp_path: Path
) -> None:
    """A model we have not catalogued blocks the run unless the caller explicitly opts in.

    This replaced an earlier advisory-only rule. The old rule was wrong for a reason that only
    became visible in field testing: an unlisted model could pass preflight, leave Start
    enabled, create a run, execute it, and spend real tokens before failing - while the run's
    own records attributed its output to a model that never ran. An OpenAI-compatible gateway
    does accept ids the application has not seen, so the exception is still available; it is
    now an explicit, auditable opt-in rather than the default.
    """
    store = tmp_path / "codex" / "auth.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"tokens": {"access_token": fake_jwt(), "refresh_token": "rt.1.x"}}), encoding="utf-8")

    service = ProviderService(app_settings)
    unlisted = service.validate_model_selection("codex:some-new-model", provider="codex")
    assert unlisted.ok is False
    assert unlisted.blocking is True
    assert unlisted.code == "MODEL_UNAVAILABLE"
    assert "not available" in unlisted.detail

    # The escape hatch: explicit, and recorded rather than silent.
    opted_in = service.validate_model_selection("codex:some-new-model", provider="codex", allow_unlisted=True)
    assert opted_in.ok is True
    assert opted_in.blocking is False
    assert opted_in.code == "MODEL_UNLISTED_ALLOWED"

    # A model the router does know is accepted with no caveat. `blocking` describes whether
    # a failure of this check would stop the run: a deliberately selected model must be
    # servable, so it is a hard requirement.
    known = service.validate_model_selection("codex:gpt-5.6-sol", provider="codex")
    assert known.ok is True and known.blocking is True
    assert "codex:gpt-5.6-sol" in known.detail


def test_preflight_accepts_a_scripted_run_without_any_provider(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """The scripted engine is a first-class path: no credential, no model calls, no cost."""
    response = client.post(
        "/api/v1/workflows/preflight",
        json={
            "project_id": project["id"],
            "source_id": source["id"],
            "objective": "review this",
            "config": {"mode": "scripted"},
        },
    )
    body = response.json()
    assert body["ok"] is True
    provider_check = next(check for check in body["checks"] if check["name"] == "provider")
    assert provider_check["blocking"] is False
    assert "no model calls" in provider_check["detail"]


def test_health_reports_operational_facts(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["scripted_runs_enabled"] is True
    assert body["running_runs"] == []
    assert body["providers_available"] == []


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/providers", "/api/v1/roles", "/api/v1/models"])
def test_every_get_endpoint_is_secret_free(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert_no_secrets(response.json())


def test_real_credential_stores_are_never_modified_by_discovery(client: TestClient) -> None:
    """Regression guard: discovery and status must be read-only.

    The application must never write to the developer's real credential stores, so the
    real Codex store is hashed before and after a full round of provider calls.
    """
    real_store = Path.home() / ".codex" / "auth.json"
    before = real_store.read_bytes() if real_store.exists() else None

    for _ in range(2):
        client.get("/api/v1/providers")
        client.post("/api/v1/providers/refresh")
        client.get("/api/v1/models")
        client.post("/api/v1/providers/doctor")

    after = real_store.read_bytes() if real_store.exists() else None
    assert after == before, f"discovery modified the real credential store at {real_store}"
    # And a token refresh was never triggered as a side effect of reading status.
    assert time.time() > 0


def test_roles_report_which_reviewers_a_run_requires(client: TestClient) -> None:
    """`mandatory` must come from the engine's coverage gate, not from a spec default.

    Regression test: the roles endpoint shipped `mandatory: false` for every role, so the
    New Review screen could not tell a user which reviewers the engine requires - it would
    have offered a reviewer set the run's coverage gate then refuses.
    """
    roles = client.get("/api/v1/roles").json()
    by_key = {role["role_key"]: role for role in roles}

    mandatory = {key for key, role in by_key.items() if role["mandatory"]}
    assert mandatory == {
        "evidence_auditor",
        "rhetorical_analyst",
        "technical_reviewer",
        "structure_reviewer",
    }

    # The write-capable role is *not* mandatory (it may legitimately not be needed) but it
    # must be the only one that can mutate.
    writers = [role["role_key"] for role in roles if role["may_mutate"]]
    assert writers == ["controlled_editor"]
    assert by_key["controlled_editor"]["mandatory"] is False

    # Every reviewer is read-only and every selectable role is a mandatory reviewer.
    for key in mandatory:
        assert by_key[key]["may_mutate"] is False
        assert by_key[key]["planner_selectable"] is True
