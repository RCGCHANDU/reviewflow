"""Live provider health check, used by ``reviewflow doctor``.

Three checks per provider, run through the real Agents SDK against the real
endpoint, because "the credential exists" and "the backend answers a structured
request" are different facts and only the second one matters:

1. **plain** - the model answers at all (catches plan gating, bad tokens, WAF blocks);
2. **structured** - ``output_type`` round-trips into a validated Pydantic model
   (the entire framework depends on this, so it is checked rather than assumed);
3. **tools** - a function tool is called and its result reaches the model (the review
   and edit loops are tool loops).

A provider whose model is plan-gated fails here rather than halfway through a
workflow, which is exactly the point: ``gpt-5.4-mini`` on a non-Pro commandcode plan
returns ``403 MODEL_NOT_IN_PLAN``, and this is where that is discovered.
"""

from __future__ import annotations

import json
from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool, set_tracing_disabled
from agents.models.interface import Model
from pydantic import BaseModel

from reviewflow.config import Settings
from reviewflow.providers.routing import ModelRouter


class DoctorProbe(BaseModel):
    """Minimal structured contract: exercises enum, int and list coercion."""

    verdict: str
    severity: int
    notes: list[str]


@function_tool
def probe_lookup(name: str) -> str:
    """Look up a named probe value and return it as JSON.

    Args:
        name: The probe key to look up.
    """
    return json.dumps({"name": name, "value": "alpha"})


async def _call(
    agent: Agent[Any],
    text: str,
    run_config: RunConfig,
    *,
    requires_streaming: bool,
    turns: int = 3,
) -> Any:
    """Run an agent over the transport the provider actually requires.

    The choice is made from the profile flag rather than by catching a failure and
    retrying: a blind retry would issue a second billed request and, worse, would mask
    the real error on a provider that rejects the request for a different reason.
    """
    if not requires_streaming:
        return await Runner.run(agent, text, max_turns=turns, run_config=run_config)
    streamed = Runner.run_streamed(agent, text, max_turns=turns, run_config=run_config)
    async for _event in streamed.stream_events():
        pass
    return streamed


async def _check_model(
    provider: str,
    model_name: str,
    model: Model,
    *,
    model_settings: ModelSettings,
    requires_streaming: bool,
) -> list[dict[str, Any]]:
    from agents.models.openai_provider import OpenAIProvider

    client = getattr(model, "_client", None) or getattr(model, "openai_client", None)
    provider_obj = OpenAIProvider(openai_client=client)
    run_config = RunConfig(model_provider=provider_obj, tracing_disabled=True)
    checks: list[dict[str, Any]] = []

    def record(check: str, ok: bool, detail: str = "") -> None:
        checks.append({"provider": provider, "model": model_name, "check": check, "ok": ok, "detail": detail})

    try:
        agent = Agent[Any](name="probe-plain", instructions="Answer in one short sentence.", model=model, model_settings=model_settings)
        result = await _call(agent, "Say hello.", run_config, requires_streaming=requires_streaming)
        record("plain completion", True, str(result.final_output)[:80])
    except Exception as exc:
        record("plain completion", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    try:
        agent = Agent[Any](
            name="probe-structured",
            instructions="Return the JSON object described by the schema.",
            model=model,
            model_settings=model_settings,
            output_type=DoctorProbe,
        )
        result = await _call(
            agent, "Verdict 'revise', severity 2, notes ['a','b'].", run_config, requires_streaming=requires_streaming
        )
        output = result.final_output
        record("structured output", isinstance(output, DoctorProbe), repr(output)[:120])
    except Exception as exc:
        record("structured output", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    try:
        agent = Agent[Any](
            name="probe-tools",
            instructions="Call probe_lookup for the requested name, then answer with its value only.",
            model=model,
            model_settings=model_settings,
            tools=[probe_lookup],
        )
        result = await _call(agent, "Look up 'alpha'.", run_config, requires_streaming=requires_streaming, turns=4)
        called = any(
            getattr(item, "type", None) == "tool_call_item"
            or getattr(getattr(item, "raw_item", None), "type", None) == "function_call"
            for item in result.new_items
        )
        record("tool call", called, f"answer={str(result.final_output)[:40]!r}")
    except Exception as exc:
        record("tool call", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    return checks


async def run_doctor(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Check every credentialed provider (or just one), and report what failed."""
    set_tracing_disabled(True)
    checks: list[dict[str, Any]] = []
    requested = (provider or settings.provider or "auto").strip().lower()

    partial = settings.model_copy(update={"provider": requested}) if provider else settings
    router = ModelRouter.from_settings(partial)
    if router is None:
        return {
            "requested_provider": requested,
            "active_provider": None,
            "checks": [],
            "passed": 0,
            "total": 0,
            "note": "no provider credential discovered",
        }

    targets = sorted(router.profiles) if provider in (None, "auto") else [router.default_provider]
    for name in targets:
        profile = router.profiles[name]
        candidates = [model] if model else [profile.models.orchestrator, profile.models.reviewer]
        for candidate in candidates:
            if candidate is None:  # pragma: no cover - defensive
                continue
            ref = f"{name}:{candidate}" if ":" not in candidate else candidate
            resolved_model = router.get_model(ref)
            model_settings = router.model_settings_for(ref)
            checks.extend(
                await _check_model(
                    name,
                    ref,
                    resolved_model,
                    model_settings=model_settings,
                    requires_streaming=router.requires_streaming_for(ref),
                )
            )

    passed = sum(1 for check in checks if check["ok"])
    return {
        "requested_provider": requested,
        "active_provider": router.default_provider,
        "checks": checks,
        "passed": passed,
        "total": len(checks),
    }
