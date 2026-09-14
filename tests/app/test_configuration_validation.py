"""Regression tests for P0-1: an unstartable configuration must never begin a run.

Required behaviour (the external-beta entry gate):

    given a valid provider and a model the provider does not report,
    then  preflight rejects it, and
          POST /workflows rejects it, and
          no workflow is created, no executor submission happens, and no model is called.

Three layers, because no single one is sufficient here:

* **service level** - the rule itself, with the provider catalogue stubbed, so the verdict
  cannot depend on whether this machine holds live provider credentials;
* **API level** - the rejection is structured and no run is persisted;
* **browser level** - see ``frontend/e2e/uat-config-gate.spec.ts``, which drives the real UI
  against a server that does hold credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from reviewflow_app.api.deps import AppState
from reviewflow_app.services.providers import ProviderService

from .conftest import start_run

LISTED_MODEL = "gpt-5.6-luna"
UNLISTED_MODEL = "definitely-not-a-real-model-xyz"


def _stub_catalogue(
    service: ProviderService, monkeypatch: Any, *, listed: list[str] | None = None
) -> None:
    """Make the provider reachable and its catalogue known, leaving only the model rule."""
    monkeypatch.setattr(service, "_router", lambda provider=None: object())
    monkeypatch.setattr(
        service,
        "models",
        lambda provider=None: [type("Row", (), {"id": model_id})() for model_id in (listed or [LISTED_MODEL])],
    )


# --------------------------------------------------------------------------- #
# Service level: the rule
# --------------------------------------------------------------------------- #


def test_unlisted_model_is_refused_by_the_rule(state: AppState, monkeypatch: Any) -> None:
    service = state.provider_service
    _stub_catalogue(service, monkeypatch)

    check = service.validate_model_selection(f"commandcode:{UNLISTED_MODEL}", provider="commandcode")

    assert check.ok is False
    assert check.blocking is True, "an unusable model must stop a run, not merely warn"
    assert check.code == "MODEL_UNAVAILABLE"
    assert "not available" in check.detail
    assert "may still work" not in check.detail, "an unsupported model must not be described as possibly working"


def test_listed_model_passes_the_rule(state: AppState, monkeypatch: Any) -> None:
    service = state.provider_service
    _stub_catalogue(service, monkeypatch)

    check = service.validate_model_selection(f"commandcode:{LISTED_MODEL}", provider="commandcode")

    assert check.ok is True
    assert check.code is None


def test_explicit_opt_in_downgrades_the_rule_to_advisory(state: AppState, monkeypatch: Any) -> None:
    service = state.provider_service
    _stub_catalogue(service, monkeypatch)

    check = service.validate_model_selection(
        f"commandcode:{UNLISTED_MODEL}", provider="commandcode", allow_unlisted=True
    )

    assert check.ok is True
    assert check.blocking is False
    assert check.code == "MODEL_UNLISTED_ALLOWED"


def test_auto_selection_is_never_a_model_failure(state: AppState, monkeypatch: Any) -> None:
    service = state.provider_service
    _stub_catalogue(service, monkeypatch)

    for selection in ("", "   ", "auto"):
        check = service.validate_model_selection(selection, provider="commandcode")
        assert check.ok is True, (selection, check)


def test_unknown_provider_is_a_configuration_failure_not_an_exception(state: AppState) -> None:
    """A bad provider name must not raise: it is a user error with a structured answer."""
    service = state.provider_service
    config = type(
        "Config",
        (),
        {"mode": "live", "provider": "does-not-exist", "role_models": {}, "allow_unlisted_models": False},
    )()

    failures = service.blocking_configuration_failures(config)

    assert failures, "an unknown provider must produce a blocking failure"
    assert failures[0].code == "UNKNOWN_PROVIDER"
    assert "expected one of" in failures[0].detail


def test_blocking_failures_do_not_blame_a_model_for_a_provider_problem(state: AppState) -> None:
    """Attribution matters: with no usable provider, the model must not be reported as the fault."""
    service = state.provider_service
    config = type(
        "Config",
        (),
        {
            "mode": "live",
            "provider": "commandcode",
            "role_models": {"evidence_auditor": f"commandcode:{UNLISTED_MODEL}"},
            "allow_unlisted_models": False,
        },
    )()

    failures = service.blocking_configuration_failures(config)

    assert failures, "a run without a usable provider cannot start"
    assert {failure.code for failure in failures} == {"PROVIDER_UNAVAILABLE"}
    assert all(not failure.name.startswith("model:") for failure in failures)


# --------------------------------------------------------------------------- #
# API level: the rejection is structured, and nothing is created
# --------------------------------------------------------------------------- #


def _post(client: TestClient, path: str, config: dict[str, Any], project_id: str, source_id: str) -> Any:
    return client.post(
        path,
        json={
            "project_id": project_id,
            "source_id": source_id,
            "objective": "configuration validation probe",
            "config": config,
        },
    )


def _run_count(client: TestClient) -> int:
    payload = client.get("/api/v1/workflows").json()
    return len(payload) if isinstance(payload, list) else len(payload.get("runs", []))


def test_unknown_provider_creation_is_rejected_with_a_structured_error(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    before = _run_count(client)
    response = _post(
        client,
        "/api/v1/workflows",
        {"mode": "live", "provider": "does-not-exist"},
        project["id"],
        source["id"],
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_configuration"
    assert body["error"]["retryable"] is False
    codes = {check.get("code") for check in body["error"]["detail"]["checks"]}
    assert "UNKNOWN_PROVIDER" in codes, codes
    assert _run_count(client) == before, "no run may be persisted for an invalid configuration"


def test_unlisted_model_creation_is_rejected_in_an_uncredentialed_environment(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """Whatever the reason, a live run that cannot execute must not be created.

    In this environment the provider has no credential, so the *provider* check is the one
    that blocks. The model-specific reason is covered by the service-level tests and by the
    browser test against a credentialed server.
    """
    before = _run_count(client)
    response = _post(
        client,
        "/api/v1/workflows",
        {"mode": "live", "provider": "commandcode", "role_models": {"evidence_auditor": f"commandcode:{UNLISTED_MODEL}"}},
        project["id"],
        source["id"],
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_configuration"
    assert _run_count(client) == before


def test_default_selection_produces_no_model_blocking_failure(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """The gate must not invent a model problem for the ordinary path."""
    response = _post(
        client,
        "/api/v1/workflows/preflight",
        {"mode": "live", "provider": "auto"},
        project["id"],
        source["id"],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [c for c in payload["checks"] if str(c["name"]).startswith("model:")] == []
    assert not any(str(f).startswith("model:") for f in payload["blocking_failures"]), payload["blocking_failures"]


def test_scripted_runs_are_unaffected_by_configuration_validation(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """A scripted run makes no model calls, so no model rule may block it."""
    run = start_run(
        client,
        project_id=project["id"],
        source_id=source["id"],
        scenario="clean_input_no_edit",
    )
    assert run["status"] in {"queued", "running"}
