"""Agent invocation with timeouts, bounded retries and failure classification.

This module is the single place where the workflow touches ``Runner.run``. It
exists so that:

* every call has a wall-clock timeout and a turn limit;
* every failure is classified (transient / invalid output / timeout / logical /
  guardrail / infrastructure) instead of collapsing into a string;
* retries are bounded and feedback-driven rather than blind;
* tests can substitute a fake invoker without touching the engine;
* tests can also keep the real ``Runner`` and substitute the *model* instead.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agents import Agent, ModelBehaviorError, ModelProvider, RunConfig, Runner
from agents.exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    ToolTimeoutError,
)
from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, RateLimitError

from reviewflow.config import Settings
from reviewflow.context import WorkflowContext
from reviewflow.errors import AgentInvocationError
from reviewflow.models.enums import FailureKind, Stage
from reviewflow.observability.logging_setup import get_logger
from reviewflow.orchestration.limits import RetryPolicy
from reviewflow.prompts import corrective_feedback
from reviewflow.validation import ValidationOutcome

_log = get_logger("invoker")


async def execute_run(
    agent: Agent[Any],
    input_text: str,
    *,
    context: Any = None,
    max_turns: int,
    run_config: RunConfig,
    requires_streaming: bool = False,
) -> Any:
    """Run one agent over the transport its provider requires.

    Every model call in the framework goes through here - the stage invoker, the input
    guardrail, and the agent-as-tool adjudicator - because "which transport" is a
    property of the *backend*, not of the caller. The Codex backend answers a
    non-streaming request with ``400 {"detail": "Stream must be set to true"}``, so a
    caller that forgets to stream does not merely lose a nicety: the call fails. Having
    one helper is what keeps that from being re-learned at each call site (it was,
    twice, before a live run surfaced it).

    Returns either a ``RunResult`` or a completed ``RunResultStreaming``; both expose
    ``final_output`` and ``context_wrapper.usage``, which is all callers read.
    """
    if not requires_streaming:
        return await Runner.run(agent, input_text, context=context, max_turns=max_turns, run_config=run_config)
    streamed = Runner.run_streamed(agent, input_text, context=context, max_turns=max_turns, run_config=run_config)
    # The run only completes once every event is consumed; draining is required, not
    # optional, and the result is unusable before that point.
    async for _event in streamed.stream_events():
        pass
    return streamed


def requires_streaming_for(provider: Any, model: Any) -> bool:
    """Ask a provider whether it needs streaming for this model.

    Duck-typed on purpose: the scripted test provider injected by the suite has no such
    concept and must keep taking the ordinary path. A non-string model (an already-built
    ``Model`` object) cannot be resolved by name, and the SDK-protocol routers only
    resolve strings, so it returns ``False`` rather than guessing.
    """
    checker = getattr(provider, "requires_streaming_for", None)
    if not callable(checker) or not isinstance(model, str):
        return False
    return bool(checker(model))

#: Exception types that indicate a retryable transport/provider problem.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)


@dataclass
class InvocationRequest:
    """Everything needed for one agent call."""

    agent: Agent[Any]
    role_key: str
    stage: Stage
    context: WorkflowContext
    input_text: str
    round_index: int = 0
    #: Optional semantic validator run on the structured output before it is accepted.
    validator: Callable[[Any], ValidationOutcome] | None = None
    timeout_s: float | None = None
    max_turns: int | None = None


@dataclass
class InvocationResult:
    """Outcome of an agent call, successful or not."""

    role_key: str
    stage: Stage
    ok: bool
    output: Any | None = None
    failure_kind: FailureKind | None = None
    error: str | None = None
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str | None = None
    validation_notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def retried(self) -> bool:
        return self.attempts > 1


class AgentInvoker(Protocol):
    """The injection seam. The engine depends only on this."""

    async def invoke(self, request: InvocationRequest) -> InvocationResult: ...


class RunConfigFactory:
    """Builds per-call ``RunConfig`` objects with trace naming and metadata."""

    def __init__(
        self,
        settings: Settings,
        workflow_id: str,
        *,
        model_provider: ModelProvider | None = None,
    ) -> None:
        self.settings = settings
        self.workflow_id = workflow_id
        self.model_provider = model_provider

    def build(self, *, stage: Stage, role_key: str, round_index: int) -> RunConfig:
        # ``model_provider`` is only passed when set, so the SDK's own default
        # provider is used otherwise.
        provider_kwargs: dict[str, Any] = (
            {"model_provider": self.model_provider} if self.model_provider is not None else {}
        )
        return RunConfig(
            **provider_kwargs,
            workflow_name=f"{self.settings.tracing.workflow_name}:{self.workflow_id}",
            group_id=self.workflow_id,
            trace_metadata={
                "workflow_id": self.workflow_id,
                "stage": stage.value,
                "role": role_key,
                "round": round_index,
            },
            tracing_disabled=not self.settings.tracing.enabled,
            trace_include_sensitive_data=self.settings.tracing.include_sensitive_data,
        )


class SdkAgentInvoker:
    """Calls ``Runner.run`` with classification, timeouts and bounded retries.

    ``router`` is optional and only ever *adds* behaviour: when the routed provider
    refuses non-streaming requests (the Codex OAuth backend answers a non-streaming
    call with ``Stream must be set to true``), the call is issued through
    ``Runner.run_streamed`` and its events are drained to completion. Without a
    router nothing changes, which keeps the scripted test path identical.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        run_config_factory: RunConfigFactory,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        router: Any = None,
    ) -> None:
        self.settings = settings
        self.run_config_factory = run_config_factory
        self.retry_policy = retry_policy or RetryPolicy(settings.budget.retries)
        self._sleep = sleep or asyncio.sleep
        self._on_event = on_event
        self.router = router

    # -- public API --------------------------------------------------------- #

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        timeout = request.timeout_s or self.settings.budget.agent_timeout_s
        max_turns = request.max_turns or self.settings.budget.max_turns_per_agent
        input_text = request.input_text
        attempts = 0
        last_error = ""
        kind = FailureKind.INFRASTRUCTURE

        while True:
            attempts += 1
            started = time.monotonic()
            # A start signal is what lets an observer show *which* agents are running:
            # the completion event alone cannot distinguish "queued behind the
            # concurrency limit" from "actually executing right now".
            self._emit(
                "agent_started",
                role_key=request.role_key,
                stage=request.stage.value,
                attempt=attempts,
                round_index=request.round_index,
                model=self._model_name(request.agent),
                agent_name=getattr(request.agent, "name", None),
            )
            try:
                result = await asyncio.wait_for(
                    self._run_once(request, input_text=input_text, max_turns=max_turns),
                    timeout=timeout,
                )
            except BaseException as exc:
                kind, last_error = self._classify(exc)
                latency_ms = int((time.monotonic() - started) * 1000)
                self._emit(
                    "agent_failure",
                    role_key=request.role_key,
                    stage=request.stage.value,
                    kind=kind.value,
                    error=last_error[:400],
                    attempts=attempts,
                    latency_ms=latency_ms,
                )
                if self.retry_policy.can_retry(kind, attempts):
                    await self._sleep(self.retry_policy.backoff_seconds(kind, attempts))
                    continue
                return InvocationResult(
                    role_key=request.role_key,
                    stage=request.stage,
                    ok=False,
                    failure_kind=kind,
                    error=last_error,
                    attempts=attempts,
                    latency_ms=latency_ms,
                    model=self._model_name(request.agent),
                )

            latency_ms = int((time.monotonic() - started) * 1000)
            usage = result.context_wrapper.usage
            output = result.final_output
            self._emit(
                "agent_completed",
                role_key=request.role_key,
                stage=request.stage.value,
                attempt=attempts,
                round_index=request.round_index,
                latency_ms=latency_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model=self._model_name(request.agent),
                agent_name=getattr(request.agent, "name", None),
            )

            outcome = request.validator(output) if request.validator is not None else ValidationOutcome()
            if not outcome.ok:
                kind = FailureKind.INVALID_OUTPUT
                last_error = "output failed application validation: " + "; ".join(outcome.problems)
                self._emit(
                    "invalid_output",
                    role_key=request.role_key,
                    stage=request.stage.value,
                    problems=outcome.problems,
                    attempts=attempts,
                )
                if self.retry_policy.can_retry(kind, attempts):
                    input_text = request.input_text + "\n\n" + corrective_feedback(outcome.problems)
                    await self._sleep(self.retry_policy.backoff_seconds(kind, attempts))
                    continue
                return InvocationResult(
                    role_key=request.role_key,
                    stage=request.stage,
                    ok=False,
                    output=output,
                    failure_kind=kind,
                    error=last_error,
                    attempts=attempts,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=latency_ms,
                    model=self._model_name(request.agent),
                    validation_notes=outcome.notes,
                )

            return InvocationResult(
                role_key=request.role_key,
                stage=request.stage,
                ok=True,
                output=output,
                attempts=attempts,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=latency_ms,
                model=self._model_name(request.agent),
                validation_notes=outcome.notes,
            )

    # -- internals ---------------------------------------------------------- #

    async def _run_once(self, request: InvocationRequest, *, input_text: str, max_turns: int) -> Any:
        """Issue one agent run over the transport the routed provider requires."""
        return await execute_run(
            request.agent,
            input_text,
            context=request.context,
            max_turns=max_turns,
            run_config=self.run_config_factory.build(
                stage=request.stage, role_key=request.role_key, round_index=request.round_index
            ),
            requires_streaming=requires_streaming_for(self.router, self._model_name(request.agent)),
        )

    def _classify(self, exc: BaseException) -> tuple[FailureKind, str]:
        if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
            return FailureKind.TIMEOUT, f"agent exceeded its {self.settings.budget.agent_timeout_s}s budget"
        if isinstance(exc, (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered)):
            return FailureKind.GUARDRAIL, f"guardrail blocked the call: {exc}"
        if isinstance(exc, (ModelBehaviorError, ModelTimeoutError, ToolTimeoutError)):
            return FailureKind.INVALID_OUTPUT, f"{type(exc).__name__}: {exc}"
        if isinstance(exc, MaxTurnsExceeded):
            return FailureKind.LOGICAL, f"turn limit reached: {exc}"
        if isinstance(exc, TRANSIENT_EXCEPTIONS):
            return FailureKind.TRANSIENT, f"{type(exc).__name__}: {exc}"
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", 0) or 0
            if status >= 500 or status == 429:
                return FailureKind.TRANSIENT, f"HTTP {status}: {exc}"
            return FailureKind.INFRASTRUCTURE, f"HTTP {status}: {exc}"
        if isinstance(exc, AgentsException):
            return FailureKind.INVALID_OUTPUT, f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (OSError, ValueError, KeyError)):
            return FailureKind.INFRASTRUCTURE, f"{type(exc).__name__}: {exc}"
        return FailureKind.LOGICAL, f"{type(exc).__name__}: {exc}"

    def _model_name(self, agent: Agent[Any]) -> str | None:
        model = getattr(agent, "model", None)
        return model if isinstance(model, str) else None

    def _emit(self, event: str, **detail: Any) -> None:
        if self._on_event is not None:
            self._on_event(event, detail)
        _log.debug(event, extra={"role": detail.get("role_key"), "stage": detail.get("stage")})


async def raise_for_result(result: InvocationResult) -> Any:
    """Turn a failed invocation into an exception, for callers that cannot continue."""
    if result.ok:
        return result.output
    raise AgentInvocationError(
        f"{result.role_key} failed during {result.stage.value}: "
        f"[{result.failure_kind.value if result.failure_kind else 'unknown'}] {result.error}"
    )
