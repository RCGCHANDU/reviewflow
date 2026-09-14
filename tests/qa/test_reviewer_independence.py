"""QA: reviewer independence (architectural claim 3).

The claim: **independent reviewers must not see each other's conclusions before synthesis.**

Coverage note: before this file, no test in the repository referenced peer-conclusion
isolation at all (`grep -rli peer tests/` → nothing). The existing tests covered the
untrusted-content boundary and 4-way concurrency, but not the thing that makes four
reviewers worth more than one.

Method: capture every agent's *input packet* and *output* during a real scripted run, then
assert that no reviewer's packet contains any other reviewer's findings - while the
synthesizer's packet does contain them. The positive control matters: without it, isolation
could be achieved by simply not sharing the material, which would be a different (and worse)
property.
"""

from __future__ import annotations

from typing import Any

import pytest

from reviewflow.evals.scenarios import build_scenarios
from reviewflow.testkit import Workspace, run_scripted


def _scenario():
    """A complete script set for the normal path, owned by the engine's own eval catalog."""
    return next(s for s in build_scenarios() if s.name == "successful_end_to_end")

REVIEWERS = ("evidence_auditor", "technical_reviewer", "rhetorical_analyst", "structure_reviewer")


def _key(agent_name: str) -> str:
    """Map an agent's display name onto its role key ('Evidence auditor' -> 'evidence_auditor')."""
    return agent_name.strip().lower().replace(" ", "_").replace("-", "_")


class CapturingInvoker:
    """Wraps the real invoker, recording every request and its result."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.packets: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}

    async def invoke(self, request: Any) -> Any:
        result = await self.inner.invoke(request)
        name = _key(request.agent.name)
        self.packets[name] = request.input_text
        if getattr(result, "ok", False) and result.output is not None:
            self.outputs[name] = result.output
        return result


async def _capture() -> tuple[Any, CapturingInvoker]:
    scenario = _scenario()
    # Workspace.build owns its own temporary directory.
    workspace = Workspace.build(material=scenario.material, max_revision_rounds=scenario.max_revision_rounds)
    scripts = scenario.scripts()
    capture: dict[str, CapturingInvoker] = {}

    def hook(workflow: Any) -> None:
        capturing = CapturingInvoker(workflow.invoker)
        capture["invoker"] = capturing
        workflow.invoker = capturing

    run = await run_scripted(
        scripts,
        workspace=workspace,
        material=scenario.material,
        max_revision_rounds=scenario.max_revision_rounds,
        on_workflow=hook,
    )
    return run, capture["invoker"]


def _issue_ids(output: Any) -> list[str]:
    issues = getattr(output, "issues", None) or []
    return [str(issue.id) for issue in issues]


def _findings_text(output: Any) -> str:
    """A distinctive fragment of a reviewer's own conclusions."""
    issues = getattr(output, "issues", None) or []
    if issues:
        return str(issues[0].description)
    return str(getattr(output, "overall_assessment", "")) or ""


@pytest.mark.asyncio
async def test_no_reviewer_packet_contains_another_reviewers_conclusions() -> None:
    """The core independence property, asserted on the real packets the reviewers received."""
    run, invoker = await _capture()
    assert run.result.status.value == "succeeded", run.result.status.value

    # Only PEER reviewers, and only pre-synthesis: downstream agents (synthesizer, editor,
    # adversarial reviewer, verifier) are *supposed* to see the findings.
    reviewer_packets = {name: invoker.packets[name] for name in REVIEWERS if name in invoker.packets}
    reported = {name for name in reviewer_packets if name in invoker.outputs}
    assert len(reported) == 4, f"expected four reviewers to report, saw {sorted(reviewer_packets)}"

    leaks: list[str] = []
    for author, output in invoker.outputs.items():
        if author not in REVIEWERS:
            continue
        for issue_id in _issue_ids(output):
            for reader, packet in reviewer_packets.items():
                if reader == author:
                    continue
                if issue_id and issue_id in packet:
                    leaks.append(f"{author}'s finding {issue_id} appears in {reader}'s packet")

    assert leaks == [], "reviewer conclusions leaked into a peer packet: " + "; ".join(leaks)


@pytest.mark.asyncio
async def test_every_reviewer_still_receives_the_shared_source_material() -> None:
    """Isolation must not be achieved by starving reviewers of the material.

    If reviewers received nothing in common, the previous test would pass trivially.
    """
    run, invoker = await _capture()
    assert run.result.status.value == "succeeded"

    material_marker = "Sparse Retrieval"
    missing = [name for name in REVIEWERS if name in invoker.packets and material_marker not in invoker.packets[name]]
    assert missing == [], f"these reviewers never received the source material: {missing}"

    # The objective is shared too, so isolation is only about conclusions.
    for name in REVIEWERS:
        packet = invoker.packets.get(name, "")
        assert packet.strip(), f"{name} received an empty packet"


@pytest.mark.asyncio
async def test_synthesis_does_receive_the_reviewer_conclusions() -> None:
    """Positive control: independence ends at synthesis, or the design would be pointless."""
    run, invoker = await _capture()
    assert run.result.status.value == "succeeded"

    synthesis_packets = {
        name: text for name, text in invoker.packets.items() if name.startswith("lead_orchestrator")
    }
    assert synthesis_packets, f"no orchestrator packet captured; saw {sorted(invoker.packets)}"

    combined = "\n".join(synthesis_packets.values())
    reviewer_findings = [
        marker
        for name, output in invoker.outputs.items()
        if name in REVIEWERS
        for marker in _issue_ids(output)
    ]
    assert reviewer_findings, "the scripted reviewers produced no findings, so this is vacuous"
    delivered = [marker for marker in reviewer_findings if marker in combined]
    assert delivered, (
        "the synthesizer received NONE of the reviewers' findings, so synthesis cannot be "
        "reconciling independent work at all"
    )
