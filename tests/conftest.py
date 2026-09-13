"""Shared fixtures.

The suite is deliberately API-free by default: the real SDK executes (Runner,
structured-output validation, tool pipeline, guardrails, timeouts) against a
scripted model, so tests exercise production code paths without spending credits.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from agents import ModelResponse
from agents.models.interface import Model, ModelProvider
from agents.usage import Usage

from reviewflow.simulate import ScriptedModel, ScriptedModelProvider
from reviewflow.testkit import DEFAULT_MATERIAL, Workspace

_AGENT_RE = re.compile(r"agent=([a-z_]+)")


@pytest.fixture
def workspace() -> Workspace:
    ws = Workspace.build(DEFAULT_MATERIAL)
    try:
        yield ws
    finally:
        ws.cleanup()


@pytest.fixture
def make_workspace() -> Callable[..., Workspace]:
    created: list[Workspace] = []

    def factory(material: str = DEFAULT_MATERIAL, **kwargs: Any) -> Workspace:
        ws = Workspace.build(material, **kwargs)
        created.append(ws)
        return ws

    yield factory
    for ws in created:
        ws.cleanup()


# --------------------------------------------------------------------------- #
# Concurrency instrumentation
# --------------------------------------------------------------------------- #


@dataclass
class RendezvousModel(Model):
    """Blocks the first N participants until N have arrived.

    If the engine ran reviewers sequentially this would time out, which is how the
    parallelism test distinguishes "concurrent" from "merely asynchronous".
    """

    inner: Model
    participants: set[str]
    required: int
    timeout: float = 3.0
    arrived: int = 0
    max_concurrent: int = 0
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timed_out: bool = False

    async def get_response(self, system_instructions: str | None, *args: Any, **kwargs: Any) -> ModelResponse:
        role = _role(system_instructions)
        in_scope = role in self.participants
        if in_scope:
            async with self._lock:
                self.arrived += 1
                self.max_concurrent = max(self.max_concurrent, self.arrived)
                if self.arrived >= self.required:
                    self._event.set()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
            except TimeoutError:
                self.timed_out = True
                raise
        try:
            return await self.inner.get_response(system_instructions, *args, **kwargs)
        finally:
            if in_scope:
                self.arrived -= 1

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def get_retry_advice(self, *args: Any, **kwargs: Any) -> None:
        return None


def _role(system_instructions: str | None) -> str:
    if not system_instructions:
        return "<none>"
    match = _AGENT_RE.search(system_instructions)
    return match.group(1) if match else "<unknown>"


class RendezvousProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> Model:
        return self.model


@pytest.fixture
def rendezvous_factory() -> Callable[..., RendezvousProvider]:
    """Build a provider that blocks reviewers until they overlap."""

    def factory(inner: ScriptedModel, participants: set[str], required: int) -> RendezvousProvider:
        return RendezvousProvider(
            RendezvousModel(inner=inner, participants=participants, required=required)
        )

    return factory


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def event_types(ledger: Any, workflow_id: str) -> list[str]:
    return [event.event_type.value for event in ledger.events(workflow_id)]


async def call_tool(tool: Any, app_context: Any, **kwargs: Any) -> Any:
    """Invoke a tool through the SDK's real pipeline (guardrails included).

    ``@tool`` returns a ``FunctionTool``, not a callable, so tests go through
    ``on_invoke_tool`` with a real ``ToolContext``. That means tool input guardrails
    run exactly as they do in production.
    """
    from agents.tool_context import ToolContext

    payload = json.dumps(kwargs)
    tool_context = ToolContext(
        context=app_context,
        tool_name=getattr(tool, "name", "tool"),
        tool_call_id="call_test",
        tool_arguments=payload,
    )
    return await tool.on_invoke_tool(tool_context, payload)


def jsonl(path: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def usage_tokens() -> Usage:  # pragma: no cover - convenience only
    return Usage(requests=1, input_tokens=1, output_tokens=1, total_tokens=2)


__all__ = [
    "RendezvousModel",
    "RendezvousProvider",
    "ScriptedModel",
    "ScriptedModelProvider",
    "event_types",
    "jsonl",
]
