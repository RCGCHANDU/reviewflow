"""QA: provider/model routing honesty (release blocker: silent model fallback = 0).

The claim under test: **the model a role is reported to use is the model actually invoked.**

Method: subclass the real `ModelRouter` so that role->model resolution is untouched, but the
transport hand-off is recorded. Every call the engine makes to the SDK therefore reports the
(provider profile, bare model) the engine *asked for*, while the workflow runs a complete
scenario from the scripted fixture. Any divergence - a placeholder model leaking to a
provider that cannot serve it, a role quietly re-routed to another provider - becomes
visible as a recorded name that the router would never have reported.

This is deliberately independent of the application's own routing test
(`tests/app/test_model_resolution.py`), which asserts the resolution path. This one asserts
what was actually requested at the boundary.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agents.items import ModelResponse
from agents.models.interface import Model

from reviewflow.agents.factory import AgentFactory
from reviewflow.agents.registry import build_default_registry
from reviewflow.evals.scenarios import build_scenarios
from reviewflow.providers.routing import COMMANDCODE_MODELS, ModelRouter
from reviewflow.testkit import run_scripted


class RecordingModel(Model):
    """Delegates to the scripted fixture model, recording which model was requested."""

    def __init__(self, inner: Model, profile: str, bare: str, log: list[tuple[str, str]]) -> None:
        self.inner = inner
        self.profile = profile
        self.bare = bare
        self.log = log

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.log.append((self.profile, self.bare))
        return await self.inner.get_response(*args, **kwargs)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.log.append((self.profile, self.bare))
        return self.inner.stream_response(*args, **kwargs)


class RecordingRouter(ModelRouter):
    """A real router whose transport hand-off is spied on."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.invoked: list[tuple[str, str]] = []
        self.requested_refs: list[str] = []
        self._inner_model: Model | None = None

    def set_inner_model(self, model: Model) -> None:
        self._inner_model = model

    def get_model(self, model_name: str | None) -> Model:
        profile, bare = self.resolve(model_name)
        self.requested_refs.append(f"{profile.name}:{bare}")
        assert self._inner_model is not None, "test must supply the fixture model"
        return RecordingModel(self._inner_model, profile.name, bare, self.invoked)


@pytest.fixture
def commandcode_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "commandcode" / "auth.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"apiKey": "test-key-not-real", "userName": "qa"}))
    monkeypatch.setenv("REVIEWFLOW_COMMANDCODE_AUTH_PATH", str(store))
    monkeypatch.setenv("REVIEWFLOW_PROVIDER", "commandcode")
    return store


@pytest.mark.asyncio
async def test_the_invoked_model_is_always_one_the_router_reports(commandcode_store: Path) -> None:
    """No role may be dispatched to a model the router would not report for some role."""
    from reviewflow.config import Settings

    settings = Settings.from_env().model_copy(update={"provider": "commandcode"})
    router = RecordingRouter.from_settings(settings)  # type: ignore[attr-defined]
    assert router is not None, "a commandcode credential store was provided, so a router must exist"
    router = RecordingRouter(router._profiles, default_provider=router.default_provider)

    scenario = next(s for s in build_scenarios() if s.name == "successful_end_to_end")
    from reviewflow.simulate import ScriptedModel

    router.set_inner_model(ScriptedModel(scenario.scripts()))

    run = await run_scripted(
        scenario.scripts(),
        material=scenario.material,
        max_revision_rounds=scenario.max_revision_rounds,
        model_provider=router,
    )
    assert run.result.status.value == "succeeded", run.result.status.value
    assert router.invoked, "the workflow made no model calls at all"

    # What the engine would report for every role in the registry.
    registry = build_default_registry()
    factory = AgentFactory(settings, registry, router=router)
    reported = {factory.model_for(role) for role in registry.specs}

    invoked = {bare for _profile, bare in router.invoked}
    # The `cheap` tier is used by roles that are NOT in the registry - the task-scope input
    # guardrail, and semantic verification. Those models are therefore absent from the roles
    # table by construction; that gap is defect QA-004, pinned by the test below. Everything
    # else must be a model the router reports for some role.
    internal_tiers = {COMMANDCODE_MODELS.cheap}
    unreported = invoked - reported - internal_tiers
    assert unreported == set(), f"models invoked but never reported for any role: {sorted(unreported)}"

    # No cross-provider fallback: every call went to the configured provider.
    assert {profile for profile, _bare in router.invoked} == {"commandcode"}

    # The historical failure mode: a provider-agnostic placeholder reaching a provider that
    # cannot serve it. Specific, because it actually happened during development.
    assert "gpt-5.4-mini" not in invoked
    assert "gpt-5.4-mini" not in "".join(router.requested_refs)

    # The run exercised the reviewer and editor models, so the assertion above is not vacuous.
    assert COMMANDCODE_MODELS.reviewer in invoked
    assert COMMANDCODE_MODELS.editor in invoked


@pytest.mark.asyncio
async def test_every_role_the_run_uses_resolves_through_the_router(commandcode_store: Path) -> None:
    """Roles must be routed (provider-qualified), never left at an unresolved default.

    A bare model name that the router merely passes through is still routed; what must not
    happen is a role whose model is unrelated to the provider profiles at all. This asserts
    the requested refs are all provider-qualified for the active provider.
    """
    from reviewflow.config import Settings
    from reviewflow.simulate import ScriptedModel

    settings = Settings.from_env().model_copy(update={"provider": "commandcode"})
    base = ModelRouter.from_settings(settings)
    assert base is not None
    router = RecordingRouter(base._profiles, default_provider=base.default_provider)

    scenario = next(s for s in build_scenarios() if s.name == "unsupported_claim_detected_and_fixed")
    router.set_inner_model(ScriptedModel(scenario.scripts()))

    await run_scripted(
        scenario.scripts(),
        material=scenario.material,
        max_revision_rounds=scenario.max_revision_rounds,
        model_provider=router,
    )

    assert router.requested_refs, "no routing decision was recorded"
    assert all(ref.startswith("commandcode:") for ref in router.requested_refs), sorted(set(router.requested_refs))


@pytest.mark.asyncio
async def test_FINDING_internal_role_calls_are_absent_from_the_ledger_and_from_usage(
    commandcode_store: Path,
) -> None:
    """DEFECT QA-004 (MEDIUM): the audit ledger and the usage summary omit real model calls.

    Measured on one scripted end-to-end run:

        model calls observed at the transport seam : 14
        model calls reported in `usage.model_calls`: 11
        `agent_call` events in the ledger          : 11

    Three invocations happened that neither the ledger nor the usage summary accounts for,
    and one of the three models in use (`deepseek/deepseek-v4-flash`, the `cheap` tier, used
    by the task-scope guardrail) appears nowhere in the ledger at all - the ledger's two
    `guardrail` events carry `agent_role: null`, `agent_name: null` and no model field.

    Consequences for the QA programme:
    * `trace_completeness` cannot reach 100% from the ledger alone: "which models ran" is not
      reconstructable.
    * `tokens_per_run` / `tokens_per_valid_issue` / the UI's "N calls, N tokens" are LOWER
      BOUNDS, not measurements, because the unreported calls' tokens are missing too.

    This test asserts the defect. It is expected to fail once the ledger records guardrail
    and semantic-check invocations - which is the signal to close QA-004.
    """
    from reviewflow.config import Settings
    from reviewflow.simulate import ScriptedModel

    settings = Settings.from_env().model_copy(update={"provider": "commandcode"})
    base = ModelRouter.from_settings(settings)
    assert base is not None
    router = RecordingRouter(base._profiles, default_provider=base.default_provider)

    scenario = next(s for s in build_scenarios() if s.name == "successful_end_to_end")
    router.set_inner_model(ScriptedModel(scenario.scripts()))
    run = await run_scripted(
        scenario.scripts(),
        material=scenario.material,
        max_revision_rounds=scenario.max_revision_rounds,
        model_provider=router,
    )
    assert run.result.status.value == "succeeded"

    calls_at_seam = len(router.invoked)
    reported = run.result.usage.model_calls
    agent_calls = [
        event
        for event in run.ledger.events(run.result.workflow_id)
        if event.event_type.value == "agent_call" and getattr(event, "model", None)
    ]

    assert calls_at_seam > reported, "the accounting gap has closed: close QA-004 if so"
    assert len(agent_calls) == reported

    # The cheap-tier model is invoked but appears nowhere in the ledger.
    invoked_models = {bare for _profile, bare in router.invoked}
    assert COMMANDCODE_MODELS.cheap in invoked_models
    ledger_blob = " ".join(event.model_dump_json() for event in run.ledger.events(run.result.workflow_id))
    assert COMMANDCODE_MODELS.cheap not in ledger_blob
