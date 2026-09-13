"""Probe the OpenAI Codex OAuth path through the real Agents SDK.

The raw HTTP probe showed ``Stream must be set to true``, so this checks the two
paths the engine could use:

* ``Runner.run`` (non-streaming) — expected to fail;
* ``Runner.run_streamed`` + ``stream_events()`` — expected to work and to yield a
  validated Pydantic ``final_output``.

Run: python tools/probe_sdk_codex.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import Agent, ModelSettings, RunConfig, Runner, set_tracing_disabled
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from pydantic import BaseModel

from reviewflow.providers.codex_oauth import codex_client_kwargs


class Probe(BaseModel):
    verdict: str
    count: int


MODEL = os.environ.get("PROBE_CODEX_MODEL", "gpt-5.6-sol")


async def main() -> int:
    set_tracing_disabled(True)
    kwargs = codex_client_kwargs()
    if kwargs is None:
        print("no codex credential found")
        return 2
    print(f"model: {MODEL}")
    print(f"base_url: {kwargs['base_url']}")
    print(f"headers: {sorted(kwargs['default_headers'])}")

    client = AsyncOpenAI(**kwargs)
    model = OpenAIResponsesModel(model=MODEL, openai_client=client)
    # The Codex backend demands store=False; the SDK defaults to store=True.
    agent = Agent(
        name="probe",
        instructions="Answer with the JSON object only.",
        model=model,
        output_type=Probe,
        model_settings=ModelSettings(store=False),
    )
    run_config = RunConfig(model_provider=OpenAIProvider(openai_client=client), tracing_disabled=True)

    # 1) non-streaming: what the engine does by default
    try:
        result = await Runner.run(agent, "Return verdict 'one' and count 1.", max_turns=2, run_config=run_config)
        print(f"[OK ] Runner.run (non-streaming): {result.final_output!r}")
        non_streaming = True
    except Exception as exc:
        print(f"[FAIL] Runner.run (non-streaming): {type(exc).__name__}: {str(exc)[:200]}")
        non_streaming = False

    # 2) streaming: the path a codex-routed engine must use
    try:
        streamed = Runner.run_streamed(agent, "Return verdict 'two' and count 2.", max_turns=2, run_config=run_config)
        events = 0
        async for _event in streamed.stream_events():
            events += 1
        out = streamed.final_output
        print(f"[OK ] Runner.run_streamed: {out!r} after {events} events")
        streaming = True
    except Exception as exc:
        print(f"[FAIL] Runner.run_streamed: {type(exc).__name__}: {str(exc)[:300]}")
        streaming = False
        out = None

    report = {
        "model": MODEL,
        "non_streaming_ok": non_streaming,
        "streaming_ok": streaming,
        "final_output": getattr(out, "model_dump", lambda: out)(),
        "usage": (
            streamed.context_wrapper.usage.total_tokens if streaming and hasattr(streamed, "context_wrapper") else None
        ),
    }
    path = Path("artifacts/provider_probe_codex_sdk.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {path}")
    return 0 if streaming else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
