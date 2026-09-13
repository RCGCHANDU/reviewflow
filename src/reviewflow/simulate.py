"""Deterministic model simulation for tests and the evaluation harness.

The point of this module is to let the *real* SDK execute for real - real
``Runner.run``, real structured-output validation, real tool calls, real
guardrails, real timeouts - while the model itself is scripted. That makes tests
fast, free and reproducible without giving up SDK fidelity.

Routing rule: every composed prompt carries a ``role=<role_key>`` marker in its
runtime block, so the scripted model can tell which agent is calling without
guessing from prose.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from agents import ModelResponse
from agents.models.interface import Model, ModelProvider
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

#: Every composed prompt carries ``agent=<agent_key>`` in its runtime block.
_AGENT_RE = re.compile(r"agent=([a-z_]+)")


class ScriptingError(RuntimeError):
    """The script ran out of payloads or a role was called that has no script."""


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Structured:
    """A payload that is serialised to JSON (used for structured outputs)."""

    value: dict[str, Any] | list[Any]


@dataclass(frozen=True)
class RawText:
    """Raw text. Deliberately *not* JSON-valid, so tests can simulate malformed output."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A function-tool call emitted by the scripted model."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = "call_1"

    def item(self, index: int) -> ResponseFunctionToolCall:
        return ResponseFunctionToolCall(
            type="function_call",
            name=self.tool_name,
            arguments=json.dumps(self.arguments),
            call_id=f"{self.call_id}_{index}",
            id=f"fc_{index}",
        )


@dataclass(frozen=True)
class Raise:
    """Raise an exception from the model call, to simulate provider/behaviour failures."""

    exception: BaseException


@dataclass(frozen=True)
class Sleep:
    """Block for a duration, to simulate a hanging provider."""

    seconds: float


Payload = Structured | RawText | ToolCall | Raise | Sleep


def structured(value: dict[str, Any] | list[Any]) -> Structured:
    return Structured(value)


def text(value: str) -> RawText:
    return RawText(value)


def call(tool_name: str, **arguments: Any) -> ToolCall:
    return ToolCall(tool_name=tool_name, arguments=arguments)


def boom(exception: BaseException) -> Raise:
    return Raise(exception=exception)


def hang(seconds: float) -> Sleep:
    return Sleep(seconds=seconds)


#: Agent key of the task-scope guardrail, so scripts can drive it explicitly.
SCOPE_GUARDRAIL_AGENT = "scope_guardrail"


def scope_allow(reason: str = "the request reviews the supplied target") -> Structured:
    """A task-scope verdict that lets the workflow start."""
    return Structured({"in_scope": True, "reason": reason, "prohibited_request": False})


def scope_deny(reason: str = "the request is not a review task", *, prohibited: bool = False) -> Structured:
    """A task-scope verdict that trips the input guardrail."""
    return Structured({"in_scope": False, "reason": reason, "prohibited_request": prohibited})


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


@dataclass
class ModelCall:
    """A record of one model call, for assertions."""

    role: str
    system: str
    input: str
    tools: list[str]
    at: float = field(default_factory=time.time)


@dataclass
class ScriptedModel(Model):
    """Replays queued payloads, routed by the role marker in the system prompt."""

    scripts: dict[str, list[Payload]]
    default: list[Payload] = field(default_factory=list)
    calls: list[ModelCall] = field(default_factory=list)
    max_calls_per_role: int | None = None

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        role = _role_of(system_instructions)
        self.calls.append(
            ModelCall(
                role=role,
                system=system_instructions or "",
                input=input if isinstance(input, str) else json.dumps(input, default=str),
                tools=[getattr(t, "name", "") for t in tools],
            )
        )
        if self.max_calls_per_role is not None:
            same_role = sum(1 for c in self.calls if c.role == role)
            if same_role > self.max_calls_per_role:
                raise ScriptingError(f"role {role!r} exceeded {self.max_calls_per_role} scripted calls")

        queue = self.scripts.get(role) or self.default
        if not queue:
            raise ScriptingError(
                f"no scripted payload left for role {role!r} (call #{len(self.calls)}); "
                f"scripts available: {sorted(self.scripts)}"
            )
        payload = queue.pop(0)
        return await self._render(payload, len(self.calls))

    async def _render(self, payload: Payload, index: int) -> ModelResponse:
        match payload:
            case Raise(exception=exc):
                raise exc
            case Sleep(seconds=seconds):
                await asyncio.sleep(seconds)
                return self._response([self._message("(slept past the timeout)")])
            case ToolCall() as tool_call:
                return self._response([tool_call.item(index)])
            case RawText(text=value):
                return self._response([self._message(value)])
            case Structured(value=value):
                return self._response([self._message(json.dumps(value))])
            case _:  # pragma: no cover - defensive
                raise ScriptingError(f"unsupported payload {payload!r}")

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:  # pragma: no cover - unused
        raise NotImplementedError("reviewflow does not stream")

    def get_retry_advice(self, *args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def _message(value: str, index: int = 1) -> ResponseOutputMessage:
        return ResponseOutputMessage(
            id=f"msg_{index}",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text=value, annotations=[])],
        )

    def _response(self, output: list[Any]) -> ModelResponse:
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=120, output_tokens=60, total_tokens=180),
            response_id=f"resp_{len(self.calls)}",
        )

    # -- assertions --------------------------------------------------------- #

    def calls_for(self, role: str) -> list[ModelCall]:
        return [c for c in self.calls if c.role == role]

    def remaining(self) -> dict[str, int]:
        return {role: len(queue) for role, queue in self.scripts.items()}

    def assert_exhausted(self) -> None:
        leftover = {role: len(q) for role, q in self.scripts.items() if q}
        if leftover:
            raise AssertionError(f"scripted payloads were never consumed: {leftover}")


class ScriptedModelProvider(ModelProvider):
    """Returns one :class:`ScriptedModel` for every agent in the run."""

    def __init__(self, model: ScriptedModel) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> Model:
        return self.model


def _role_of(system_instructions: str | None) -> str:
    if not system_instructions:
        return "<none>"
    match = _AGENT_RE.search(system_instructions)
    return match.group(1) if match else "<unknown>"


# --------------------------------------------------------------------------- #
# A scripted invoker, for pure state-machine tests
# --------------------------------------------------------------------------- #


class ScriptedInvoker:
    """An :class:`~reviewflow.orchestration.invoker.AgentInvoker` that returns canned objects.

    It preserves the SDK invoker's *contract*: it applies the request's validator
    and reports ``INVALID_OUTPUT`` when the validator rejects, so engine tests that
    use it still exercise real validation logic.
    """

    def __init__(self, scripts: dict[str, list[Any]], *, on_call: Any = None) -> None:
        self.scripts = {role: list(items) for role, items in scripts.items()}
        self.calls: list[str] = []
        self.inputs: dict[str, list[str]] = {}
        self._on_call = on_call

    async def invoke(self, request: Any) -> Any:
        from reviewflow.models.enums import FailureKind
        from reviewflow.orchestration.invoker import InvocationResult

        role = request.role_key
        self.calls.append(role)
        self.inputs.setdefault(role, []).append(request.input_text)
        if self._on_call is not None:
            self._on_call(request)
        queue = self.scripts.get(role)
        if not queue:
            return InvocationResult(
                role_key=role,
                stage=request.stage,
                ok=False,
                failure_kind=FailureKind.INFRASTRUCTURE,
                error=f"no scripted output for role {role!r}",
            )
        item = queue.pop(0)
        if isinstance(item, BaseException):
            return InvocationResult(
                role_key=role,
                stage=request.stage,
                ok=False,
                failure_kind=FailureKind.LOGICAL,
                error=f"{type(item).__name__}: {item}",
            )
        if isinstance(item, InvocationResult):  # caller supplied a full result
            return item
        if request.validator is not None:
            outcome = request.validator(item)
            if not outcome.ok:
                return InvocationResult(
                    role_key=role,
                    stage=request.stage,
                    ok=False,
                    output=item,
                    failure_kind=FailureKind.INVALID_OUTPUT,
                    error="; ".join(outcome.problems),
                    validation_notes=outcome.notes,
                )
        return InvocationResult(
            role_key=role,
            stage=request.stage,
            ok=True,
            output=item,
            input_tokens=100,
            output_tokens=50,
            latency_ms=5,
            model="scripted",
        )


def assert_role_markers(models: ScriptedModel, expected: set[str]) -> None:
    """Fail when a model call arrived without a recognised role marker."""
    seen = {c.role for c in models.calls}
    unknown = seen - expected - {"<unknown>", "<none>"}
    if unknown:
        raise AssertionError(f"unexpected role markers in prompts: {sorted(unknown)}")

