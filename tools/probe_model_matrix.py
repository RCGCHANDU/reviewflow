"""Verify candidate models per provider: plain, structured, tools.

Default models must be evidence-backed, not guessed. This runs the same three
checks the framework needs against each candidate on each usable provider.

Run: python tools/probe_model_matrix.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from pydantic import BaseModel

from reviewflow.providers.codex_oauth import codex_client_kwargs
from reviewflow.providers.commandcode import commandcode_client_kwargs


class Review(BaseModel):
    verdict: str
    severity: int


@function_tool
def read_thing(name: str) -> str:
    """Read a named thing and return its contents."""
    return json.dumps({"name": name, "value": "alpha"})


CANDIDATES: dict[str, list[str]] = {
    "commandcode": ["deepseek/deepseek-v4-flash", "gpt-5.4-mini", "gpt-5.6-luna", "gpt-5.6-sol"],
    "codex": ["gpt-5.6-luna", "gpt-5.6-sol"],
}


async def check(provider: str, client: AsyncOpenAI, model_name: str) -> dict[str, object]:
    if provider == "codex":
        model = OpenAIResponsesModel(model=model_name, openai_client=client)
        settings = ModelSettings(store=False)
        streaming = True
    else:
        model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        settings = ModelSettings()
        streaming = False

    provider_obj = OpenAIProvider(openai_client=client)
    run_config = RunConfig(model_provider=provider_obj, tracing_disabled=True)
    record: dict[str, object] = {"provider": provider, "model": model_name}
    tokens = 0

    async def run(agent: Agent, text: str, turns: int = 3):
        nonlocal tokens
        if streaming:
            streamed = Runner.run_streamed(agent, text, max_turns=turns, run_config=run_config)
            async for _ in streamed.stream_events():
                pass
            tokens += streamed.context_wrapper.usage.total_tokens
            return streamed
        result = await Runner.run(agent, text, max_turns=turns, run_config=run_config)
        tokens += result.context_wrapper.usage.total_tokens
        return result

    try:
        agent = Agent(name="p", instructions="Answer in one short sentence.", model=model, model_settings=settings)
        await run(agent, "Say hello.")
        record["plain"] = True
    except Exception as exc:
        record["plain"] = False
        record["plain_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    try:
        agent = Agent(
            name="s",
            instructions="Return the JSON object described by the schema.",
            model=model,
            model_settings=settings,
            output_type=Review,
        )
        result = await run(agent, "Verdict 'revise', severity 2.")
        record["structured"] = bool(getattr(result.final_output, "verdict", None))
    except Exception as exc:
        record["structured"] = False
        record["structured_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    try:
        agent = Agent(
            name="t",
            instructions="Call read_thing for the requested name, then answer with its value only.",
            model=model,
            model_settings=settings,
            tools=[read_thing],
        )
        result = await run(agent, "Read 'alpha'.", turns=4)
        called = any(
            getattr(getattr(item, "raw_item", None), "type", None) == "function_call"
            or getattr(item, "type", None) in {"tool_call_item"}
            for item in result.new_items
        )
        record["tools"] = called
        record["tools_answer"] = str(result.final_output)[:40]
    except Exception as exc:
        record["tools"] = False
        record["tools_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    record["tokens"] = tokens
    tri = (record.get("plain"), record.get("structured"), record.get("tools"))
    record["all_ok"] = all(bool(x) for x in tri)
    print(
        f"  {model_name:<28} plain={record.get('plain')} structured={record.get('structured')} "
        f"tools={record.get('tools')} tokens={tokens}"
    )
    return record


async def main() -> int:
    set_tracing_disabled(True)
    results: list[dict[str, object]] = []

    cc = commandcode_client_kwargs()
    if cc:
        print("=== commandcode (chat_completions) ===")
        client = AsyncOpenAI(**cc)
        for name in CANDIDATES["commandcode"]:
            results.append(await check("commandcode", client, name))
    else:
        print("commandcode: no credential")

    codex = codex_client_kwargs()
    if codex:
        print("=== codex (responses, streaming) ===")
        client = AsyncOpenAI(**codex)
        for name in CANDIDATES["codex"]:
            results.append(await check("codex", client, name))
    else:
        print("codex: no credential")

    path = Path("artifacts/provider_model_matrix.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {path}")
    ok = [r for r in results if r.get("all_ok")]
    print(f"{len(ok)}/{len(results)} models fully usable")
    for r in results:
        if not r.get("all_ok"):
            print(f"  unusable: {r['provider']}:{r['model']} -> " f"{ {k: v for k, v in r.items() if 'error' in str(k)} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
