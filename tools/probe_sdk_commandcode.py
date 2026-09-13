"""Probe commandcode through the real Agents SDK.

commandcode speaks Chat Completions, so the engine reaches it with
``OpenAIChatCompletionsModel`` over an ``AsyncOpenAI`` pointed at its base URL.
The framework needs three things from it: a non-streaming call, a validated
structured output, and a working tool loop.

Run: python tools/probe_sdk_commandcode.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import Agent, RunConfig, Runner, function_tool, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI
from pydantic import BaseModel

from reviewflow.providers.commandcode import commandcode_client_kwargs


class Probe(BaseModel):
    verdict: str
    count: int
    reasons: list[str]


@function_tool
def lookup_temperature(city: str) -> str:
    """Return the recorded temperature for a city."""
    return json.dumps({"city": city, "celsius": 31})


MODEL = os.environ.get("PROBE_COMMANDCODE_MODEL", "deepseek/deepseek-v4-flash")


async def main() -> int:
    set_tracing_disabled(True)
    kwargs = commandcode_client_kwargs()
    if kwargs is None:
        print("no commandcode credential found")
        return 2
    print(f"model: {MODEL}")
    print(f"base_url: {kwargs['base_url']}")

    client = AsyncOpenAI(**kwargs)
    model = OpenAIChatCompletionsModel(model=MODEL, openai_client=client)
    run_config = RunConfig(model_provider=OpenAIProvider(openai_client=client), tracing_disabled=True)
    report: dict[str, object] = {"model": MODEL, "base_url": kwargs["base_url"]}

    # 1) plain text
    try:
        plain = Agent(name="plain", instructions="Answer in one short sentence.", model=model)
        result = await Runner.run(plain, "Say hello.", max_turns=2, run_config=run_config)
        print(f"[OK ] plain completion: {str(result.final_output)[:80]!r}")
        report["plain_ok"] = True
        report["usage"] = result.context_wrapper.usage.total_tokens
    except Exception as exc:
        print(f"[FAIL] plain completion: {type(exc).__name__}: {str(exc)[:200]}")
        report["plain_ok"] = False

    # 2) structured output
    try:
        structured = Agent(
            name="structured",
            instructions="Return the JSON object described by the schema.",
            model=model,
            output_type=Probe,
        )
        result = await Runner.run(structured, "Verdict 'ok', count 3, reasons ['a','b'].", max_turns=2, run_config=run_config)
        print(f"[OK ] structured output: {result.final_output!r}")
        report["structured_ok"] = True
    except Exception as exc:
        print(f"[FAIL] structured output: {type(exc).__name__}: {str(exc)[:200]}")
        report["structured_ok"] = False

    # 3) tool loop
    try:
        tooled = Agent(
            name="tooled",
            instructions="Use the lookup_temperature tool, then answer with the number only.",
            model=model,
            tools=[lookup_temperature],
        )
        result = await Runner.run(tooled, "What is the temperature in Chennai?", max_turns=4, run_config=run_config)
        text = str(result.final_output)
        used = any(getattr(item, "type", "") == "function_call" for item in result.new_items)
        print(f"[OK ] tool loop: {text[:60]!r} (tool called: {used})")
        report["tool_loop_ok"] = True
        report["tool_was_called"] = used
    except Exception as exc:
        print(f"[FAIL] tool loop: {type(exc).__name__}: {str(exc)[:200]}")
        report["tool_loop_ok"] = False

    path = Path("artifacts/provider_probe_commandcode_sdk.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {path}")
    return 0 if all(report.get(k) for k in ("plain_ok", "structured_ok", "tool_loop_ok")) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
