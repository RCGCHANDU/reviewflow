"""Test/eval support: scripted-model plumbing plus tool-call helpers.

This module is shipped (not test-only) because the evaluation harness depends on
it: deterministic, API-free runs are part of the product's quality story, not just
of its test suite.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from reviewflow.agents import build_default_registry
from reviewflow.config import Budget, ModelsByRole, RetryLimits, Settings, TracingSettings
from reviewflow.orchestration.workflow import ReviewWorkflow
from reviewflow.persistence.memory import InMemoryLedger
from reviewflow.simulate import ScriptedModel, ScriptedModelProvider

#: Default material used by evals and the example, unless a scenario overrides it.
#: The claim sentence is kept on one physical line on purpose: ``apply_edit`` matches
#: bytes exactly, so a phrase that wraps is not a valid ``old_text``.
DEFAULT_MATERIAL = """\
# Sparse Retrieval for Long-Video Question Answering

## Abstract

We introduce a sparse retrieval front-end for long-video question answering that
selects 12 keyframes per query. On the Haystack-LVBench 3600-second split, our method reaches 97.3% answer accuracy.

## Method

We sample candidate frames at 1 fps and score each frame with a CLIP-style
encoder. The top-12 frames are passed to a vision-language model.

## Limitations

Frame selection is bounded by the encoder's vocabulary, and we do not claim that
our results transfer to other benchmarks.

## Conclusion

Sparse selection is sufficient for long-video understanding.
"""

#: The claim used by the fix scenarios; it appears verbatim on one line.
UNSUPPORTED_CLAIM = "our method reaches 97.3% answer accuracy"
#: The smallest defensible fix for an uncited figure.
HEDGED_CLAIM = "our method reaches 97.3% answer accuracy (this figure is preliminary and uncited)"


async def call_tool(tool: Any, app_context: Any, **kwargs: Any) -> Any:
    """Invoke a tool through the SDK's real pipeline, guardrails included.

    ``@tool`` returns a ``FunctionTool`` rather than a callable, so tests go through
    ``on_invoke_tool`` with a real ``ToolContext``. Tool input guardrails therefore
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


def default_settings(root: Path, *, max_revision_rounds: int = 2, timeout_s: float = 10.0) -> Settings:
    """A fast, tracing-off configuration for scripted runs."""
    return Settings(
        workspace_root=root,
        allowed_write_roots=[root],
        ledger_path=root / "artifacts" / "audit.sqlite3",
        models=ModelsByRole(),
        budget=Budget(
            max_revision_rounds=max_revision_rounds,
            agent_timeout_s=timeout_s,
            retries=RetryLimits(transient=1, invalid_output=1, timeout=0),
        ),
        tracing=TracingSettings(enabled=False),
    ).resolved()


@dataclass
class Workspace:
    """A throwaway workspace with one target artifact."""

    root: Path
    target: Path
    settings: Settings
    material: str

    @classmethod
    def build(
        cls,
        material: str = DEFAULT_MATERIAL,
        *,
        filename: str = "manuscript.md",
        budget: Budget | None = None,
        settings: Settings | None = None,
        max_revision_rounds: int = 2,
        timeout_s: float = 10.0,
    ) -> Workspace:
        root = Path(tempfile.mkdtemp(prefix="reviewflow-eval-"))
        target = root / filename
        target.write_bytes(material.encode("utf-8"))
        if settings is not None:
            resolved = settings.model_copy(
                update={
                    "workspace_root": root,
                    "allowed_write_roots": [root],
                    "ledger_path": root / "artifacts" / "audit.sqlite3",
                }
            ).resolved()
        else:
            base = default_settings(root, max_revision_rounds=max_revision_rounds, timeout_s=timeout_s)
            resolved = base.model_copy(update={"budget": budget}) if budget else base
        return cls(root=root, target=target, settings=resolved, material=material)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def text(self) -> str:
        return self.target.read_bytes().decode("utf-8")


@dataclass
class ScriptedRun:
    """A completed scripted run, with everything a test needs to assert on."""

    result: Any
    ledger: InMemoryLedger
    model: ScriptedModel
    workspace: Workspace
    events: list = field(default_factory=list)


async def run_scripted(
    scripts: dict[str, list],
    *,
    material: str = DEFAULT_MATERIAL,
    workspace: Workspace | None = None,
    objective: str = "Find unsupported or incoherent claims and fix the smallest defensible set.",
    max_revision_rounds: int | None = None,
    settings: Settings | None = None,
    constraints: list[str] | None = None,
    verification_config: dict[str, object] | None = None,
    model_provider: Any = None,
    on_workflow: Any = None,
) -> ScriptedRun:
    """Run the real workflow against a scripted model.

    ``on_workflow`` receives the constructed workflow before it runs, so tests can
    observe engine internals (locks, invoker wrapping) without patching the engine.

    Explicit ``settings``/``max_revision_rounds`` are applied to a supplied
    ``workspace`` rather than ignored: an override that silently did nothing would
    make a test assert against the wrong configuration.
    """
    ws = workspace or Workspace.build(
        material,
        settings=settings,
        max_revision_rounds=2 if max_revision_rounds is None else max_revision_rounds,
    )
    if workspace is not None and (settings is not None or max_revision_rounds is not None):
        base = settings.model_copy() if settings is not None else workspace.settings
        if max_revision_rounds is not None:
            base = base.model_copy(
                update={"budget": base.budget.model_copy(update={"max_revision_rounds": max_revision_rounds})}
            )
        ws = replace(
            workspace,
            settings=base.model_copy(
                update={
                    "workspace_root": workspace.root,
                    "allowed_write_roots": [workspace.root],
                    "ledger_path": workspace.root / "artifacts" / "audit.sqlite3",
                }
            ).resolved(),
        )
    model = ScriptedModel(scripts)
    provider = model_provider or ScriptedModelProvider(model)
    ledger = InMemoryLedger()
    workflow = ReviewWorkflow(
        ws.settings,
        ledger=ledger,
        registry=build_default_registry(),
        model_provider=provider,
    )
    if on_workflow is not None:
        on_workflow(workflow)
    result = await workflow.run(
        objective=objective,
        target=ws.target,
        constraints=constraints,
        verification_config=verification_config,
    )
    return ScriptedRun(
        result=result, ledger=ledger, model=model, workspace=ws, events=ledger.events(result.workflow_id)
    )
