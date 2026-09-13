"""Which model does each role actually run on?

The application promises that model choice comes from the provider layer. This file holds
the regression test for the seam where that promise was broken: the run service injected the
router as the *transport* provider, and the workflow then left ``AgentFactory`` without a
router, so every role fell back to ``ModelsByRole``'s provider-agnostic placeholders. On this
machine that meant reviewers were sent to ``gpt-5.4-mini`` against a gateway whose account
cannot use it (``403 MODEL_NOT_IN_PLAN``), and every live run failed at the first reviewer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewflow.agents.factory import AgentFactory
from reviewflow.orchestration.workflow import ReviewWorkflow
from reviewflow.providers.routing import COMMANDCODE_MODELS, ModelRouter
from reviewflow_app.api.deps import AppState
from reviewflow_app.config import AppSettings
from reviewflow_app.schemas import RunConfigSnapshot

REVIEWERS = ("evidence_auditor", "rhetorical_analyst", "technical_reviewer", "structure_reviewer")


@pytest.fixture
def commandcode_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated commandcode credential store, so no real login is read or written."""
    store = tmp_path / "commandcode" / "auth.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"apiKey": "test-key-not-a-real-secret", "userName": "tester"}))
    monkeypatch.setenv("REVIEWFLOW_COMMANDCODE_AUTH_PATH", str(store))
    monkeypatch.setenv("REVIEWFLOW_PROVIDER", "commandcode")
    return store


def test_the_apps_run_path_resolves_roles_through_the_provider_profile(
    tmp_path: Path, commandcode_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow built the way the run service builds it must route roles by provider."""
    settings = AppSettings(data_root=tmp_path / "app", static_dir=None)
    state = AppState.build(settings)
    router = ModelRouter.from_settings(state.run_service.build_settings({
        "provider": "commandcode",
        "config": RunConfigSnapshot(mode="live", provider="commandcode").model_dump(mode="json"),
        "workspace_root": str(settings.run_workspace("test")),
        "id": "run_test",
    }))
    assert router is not None
    assert router.default_provider == "commandcode"  # the profile the router resolved to

    # Exactly how RunService.execute hands the router to the engine.
    workflow = ReviewWorkflow(state.run_service.build_settings({
        "provider": "commandcode",
        "config": RunConfigSnapshot(mode="live", provider="commandcode").model_dump(mode="json"),
        "workspace_root": str(settings.run_workspace("test")),
        "id": "run_test",
    }), registry=None, model_provider=router)

    assert workflow.router is not None, "an injected router must also be the model router"

    resolved: dict[str, str] = {role: workflow.factory.model_for(role) for role in REVIEWERS}
    for role, model in resolved.items():
        assert model == COMMANDCODE_MODELS.reviewer, f"{role} resolved to {model!r}"
        assert model != "gpt-5.4-mini", "the placeholder default must never reach a provider that cannot serve it"

    # The two write/verify roles differ from reviewers, and still come from the profile.
    assert workflow.factory.model_for("controlled_editor") == COMMANDCODE_MODELS.editor
    assert workflow.factory.model_for("adversarial_reviewer") == COMMANDCODE_MODELS.adversarial
    assert workflow.factory.model_for("verifier") == COMMANDCODE_MODELS.verifier


def test_the_transport_provider_and_the_model_router_agree(tmp_path: Path, commandcode_store: Path) -> None:
    """Whatever the events report as the model must be what the router would choose.

    The UI reads the model from agent events and from the roles endpoint. If those two
    disagree, one of them is lying to the user - which is what happened before the fix.
    """
    settings = AppSettings(data_root=tmp_path / "app", static_dir=None)
    state = AppState.build(settings)
    run = {
        "provider": "commandcode",
        "config": RunConfigSnapshot(mode="live", provider="commandcode").model_dump(mode="json"),
        "workspace_root": str(settings.run_workspace("test")),
        "id": "run_test",
    }
    engine_settings = state.run_service.build_settings(run)
    router = ModelRouter.from_settings(engine_settings)
    assert router is not None

    factory = AgentFactory(engine_settings, state.run_service.build_registry(RunConfigSnapshot(mode="live")), router=router)
    reported_by_roles_endpoint: dict[str, str] = {
        role.role_key: role.default_model for role in state.provider_service.roles(provider="commandcode")
    }

    # Every role the registry builds must be reported by the endpoint identically. The
    # verifier is not in that set: verification is the deterministic runner, and 'verifier'
    # is a model *tier* rather than an agent the registry constructs.
    for role in (*REVIEWERS, "controlled_editor", "adversarial_reviewer", "diff_reviewer"):
        assert factory.model_for(role) == reported_by_roles_endpoint[role]


def test_run_settings_are_built_from_the_snapshot(tmp_path: Path, commandcode_store: Path) -> None:
    """The snapshot a run is started with must be what its engine actually runs with."""
    settings = AppSettings(data_root=tmp_path / "app", static_dir=None)
    state = AppState.build(settings)
    snapshot = RunConfigSnapshot(
        mode="live",
        provider="commandcode",
        role_models={"evidence_auditor": "commandcode:gpt-5.6-sol"},
        limits={"max_revision_rounds": 1, "max_reviewer_concurrency": 2},
    )
    engine_settings = state.run_service.build_settings(
        {
            "provider": "commandcode",
            "config": snapshot.model_dump(mode="json"),
            "workspace_root": str(settings.run_workspace("test")),
            "id": "run_test",
        }
    )

    assert engine_settings.provider == "commandcode"
    assert engine_settings.budget.max_revision_rounds == 1
    assert engine_settings.budget.max_reviewer_concurrency == 2

    # The pin travels as a model override, which is what the router consults first; the
    # `models` table stays the provider-agnostic placeholder table by design.
    router = ModelRouter.from_settings(engine_settings)
    assert router is not None
    factory = AgentFactory(engine_settings, state.run_service.build_registry(snapshot), router=router)
    assert factory.model_for("evidence_auditor") == "commandcode:gpt-5.6-sol"
    # A role the user did not pin still comes from the provider profile.
    assert factory.model_for("technical_reviewer") == COMMANDCODE_MODELS.reviewer
