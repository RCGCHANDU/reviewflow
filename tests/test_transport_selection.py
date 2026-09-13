"""Transport selection: streaming must follow the provider, not the caller.

This is the regression test for a defect a live Codex run exposed: the stage invoker
streamed correctly, but the input guardrail and the agent-as-tool adjudicator issued
plain ``Runner.run`` calls, so on the Codex backend they failed with
``Stream must be set to true`` before any review work happened. Each model call site
now routes through one helper, and these tests pin that down without a network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agents import Agent, RunConfig

from reviewflow.config import Budget, Settings
from reviewflow.guardrails.task_scope import build_scope_guardrail_agent
from reviewflow.models.enums import Stage
from reviewflow.orchestration.invoker import (
    InvocationRequest,
    RunConfigFactory,
    SdkAgentInvoker,
    execute_run,
    requires_streaming_for,
)
from reviewflow.providers.routing import ModelRouter, ProviderProfile, RoleModels

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeStreamed:
    """Stands in for ``RunResultStreaming``: events, then a final output."""

    def __init__(self, events: int = 3) -> None:
        self.final_output: Any = "streamed-output"
        self._events = events

    async def stream_events(self) -> Any:
        for index in range(self._events):
            yield {"type": "event", "index": index}


class FakeRunner:
    """Records which entry point the SDK was asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, agent: Any, text: Any, **kwargs: Any) -> Any:
        self.calls.append("run")
        return SimpleNamespace(final_output="run-output")

    def run_streamed(self, agent: Any, text: Any, **kwargs: Any) -> FakeStreamed:
        self.calls.append("run_streamed")
        return FakeStreamed()


def profiling_router(*, streaming: bool) -> ModelRouter:
    profile = ProviderProfile(
        name="codex",
        api_mode="responses",
        base_url="https://codex.example/v1",
        models=RoleModels(
            orchestrator="strong",
            reviewer="mid",
            editor="strong",
            adversarial="strong",
            cheap="cheap-model",
            verifier="mid",
        ),
        requires_streaming=streaming,
        store=False,
        client_kwargs=lambda: {"api_key": "k"},
    )
    return ModelRouter({"codex": profile}, default_provider="codex")


# --------------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_execute_run_uses_the_streaming_path_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewflow.orchestration import invoker

    fake = FakeRunner()
    monkeypatch.setattr(invoker, "Runner", fake)
    agent: Agent[Any] = Agent(name="probe", instructions="x")

    result = await execute_run(agent, "hello", max_turns=1, run_config=RunConfig(), requires_streaming=True)

    assert fake.calls == ["run_streamed"], "a streaming-only backend must never get a plain run"
    assert result.final_output == "streamed-output", "events must be drained before the result is used"


@pytest.mark.asyncio
async def test_execute_run_keeps_the_simple_path_when_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewflow.orchestration import invoker

    fake = FakeRunner()
    monkeypatch.setattr(invoker, "Runner", fake)
    agent: Agent[Any] = Agent(name="probe", instructions="x")

    result = await execute_run(agent, "hello", max_turns=1, run_config=RunConfig(), requires_streaming=False)

    assert fake.calls == ["run"]
    assert result.final_output == "run-output"


@pytest.mark.asyncio
async def test_streaming_is_not_required_by_a_provider_that_has_no_opinion() -> None:
    class Scripted:
        pass

    assert requires_streaming_for(Scripted(), "any-model") is False
    assert requires_streaming_for(None, "any-model") is False
    # An already-built model object cannot be resolved by name, so it must not guess.
    assert requires_streaming_for(profiling_router(streaming=True), object()) is False


def test_requires_streaming_follows_the_router_for_a_model_name() -> None:
    assert requires_streaming_for(profiling_router(streaming=True), "strong") is True
    assert requires_streaming_for(profiling_router(streaming=False), "strong") is False


# --------------------------------------------------------------------------- #
# the invoker
# --------------------------------------------------------------------------- #


def invoker_for(router: ModelRouter | None) -> SdkAgentInvoker:
    settings = Settings().model_copy(update={"budget": Budget(agent_timeout_s=30.0)})
    return SdkAgentInvoker(settings, run_config_factory=RunConfigFactory(settings, "wf"), router=router)


@pytest.mark.asyncio
async def test_invoker_streams_for_a_provider_that_requires_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewflow.orchestration import invoker as module

    fake = FakeRunner()
    monkeypatch.setattr(module, "Runner", fake)
    router = profiling_router(streaming=True)
    agent: Agent[Any] = Agent(name="planner", instructions="x", model="strong")

    result = await invoker_for(router)._run_once(
        InvocationRequest(agent=agent, role_key="orchestrator", stage=Stage.PLAN, context=None, input_text="go"),
        input_text="go",
        max_turns=1,
    )

    assert fake.calls == ["run_streamed"]
    assert result.final_output == "streamed-output"


@pytest.mark.asyncio
async def test_invoker_does_not_stream_for_an_ordinary_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from reviewflow.orchestration import invoker as module

    fake = FakeRunner()
    monkeypatch.setattr(module, "Runner", fake)
    agent: Agent[Any] = Agent(name="planner", instructions="x", model="strong")

    result = await invoker_for(profiling_router(streaming=False))._run_once(
        InvocationRequest(agent=agent, role_key="orchestrator", stage=Stage.PLAN, context=None, input_text="go"),
        input_text="go",
        max_turns=1,
    )

    assert fake.calls == ["run"]
    assert result.final_output == "run-output"


@pytest.mark.asyncio
async def test_invoker_without_a_router_behaves_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scripted test path must be untouched by provider routing."""
    from reviewflow.orchestration import invoker as module

    fake = FakeRunner()
    monkeypatch.setattr(module, "Runner", fake)
    agent: Agent[Any] = Agent(name="planner", instructions="x", model="strong")

    await invoker_for(None)._run_once(
        InvocationRequest(agent=agent, role_key="orchestrator", stage=Stage.PLAN, context=None, input_text="go"),
        input_text="go",
        max_turns=1,
    )

    assert fake.calls == ["run"]


# --------------------------------------------------------------------------- #
# the guardrail and the agent-as-tool
# --------------------------------------------------------------------------- #


def test_guardrail_agent_uses_the_routed_cheap_model() -> None:
    """A guardrail that runs on a plan-gated model is a guardrail that stops guarding."""
    settings = Settings()
    router = profiling_router(streaming=True)
    agent = build_scope_guardrail_agent(settings, router=router)
    assert agent.model == "cheap-model"
    assert agent.model_settings is not None and agent.model_settings.store is False


def test_guardrail_agent_keeps_settings_optional_for_the_scripted_path() -> None:
    """``Agent`` rejects an explicit ``model_settings=None``, so it must be omitted."""
    agent = build_scope_guardrail_agent(Settings(), router=None)
    assert agent.model == Settings().models.cheap
    assert agent.model_settings is None or agent.model_settings.store is None
