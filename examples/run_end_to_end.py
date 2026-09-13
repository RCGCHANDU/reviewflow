"""Runnable end-to-end example.

Default mode is scripted: the real workflow runs (real SDK, real tools, real
verification) against canned model responses, so it costs nothing and is
deterministic. ``--live`` switches to real model calls and uses whichever provider
credential this machine already has (ChatGPT OAuth, commandcode, or an API key).

    python examples/run_end_to_end.py
    python examples/run_end_to_end.py --live --objective "..."  --target path/to/manuscript.md
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from reviewflow.agents import build_default_registry  # noqa: E402
from reviewflow.config import Budget, ModelsByRole, Settings, TracingSettings  # noqa: E402
from reviewflow.observability import configure_logging  # noqa: E402
from reviewflow.orchestration.workflow import ReviewWorkflow  # noqa: E402
from reviewflow.persistence import open_ledger  # noqa: E402
from reviewflow.simulate import ScriptedModel, ScriptedModelProvider  # noqa: E402

MATERIAL = ROOT / "examples" / "materials" / "sample_manuscript.md"
UNSUPPORTED = "our pipeline reaches 97.3% answer accuracy"
HEDGED = "our pipeline reaches 97.3% answer accuracy (this figure is preliminary and no source is cited)"


def scripted_payloads() -> dict[str, list]:
    """The responses a model would give, one list per agent, in call order."""
    from reviewflow.fixtures import (
        apply_edit,
        change,
        diff_review,
        edit_result,
        final_report,
        finding,
        issue,
        issue_map,
        plan,
        report,
        resolution,
        verdict,
        verification,
    )
    from reviewflow.simulate import scope_allow

    return {
        "scope_guardrail": [scope_allow()],
        "planner": [
            plan(
                specialists=["evidence_auditor", "technical_reviewer", "structure_reviewer"],
                units=[
                    {
                        "unit_id": "u1",
                        "location": "L1-L25",
                        "description": "abstract and the headline accuracy claim",
                        "priority": "critical",
                    },
                    {
                        "unit_id": "u2",
                        "location": "L26-L45",
                        "description": "method and the baseline comparison",
                        "priority": "major",
                    },
                ],
            )
        ],
        "evidence_auditor": [
            report(
                "evidence_auditor",
                issues=[
                    issue(
                        "ev-1",
                        description="the headline accuracy figure is asserted with no source and drives the abstract's conclusion",
                        evidence=f"quoted: '{UNSUPPORTED}'",
                        recommendation="cite the source or mark the figure provisional and unsourced",
                    )
                ],
                assessment="the method section is sound; the abstract overstates an uncited number",
            )
        ],
        "technical_reviewer": [
            report(
                "technical_reviewer",
                issues=[
                    issue(
                        "tech-1",
                        severity="major",
                        category="incompatible_comparison",
                        description="the baseline is said to use the same model, but no version is given, so the comparison cannot be reproduced",
                        evidence="quoted: 'the same vision-language model'",
                        recommendation="state the version used for both arms",
                        location="L26-L45",
                        unit_id="u2",
                    )
                ],
            )
        ],
        "structure_reviewer": [
            report(
                "structure_reviewer",
                assessment="abstract, method, limitations and conclusion line up; the limitation is delivered",
            )
        ],
        "synthesizer": [
            issue_map(
                accepted=[resolution("evidence_auditor:ev-1")],
                rejected=[
                    resolution(
                        "technical_reviewer:tech-1",
                        disposition="rejected",
                        rationale="the sentence quoted by the reviewer does not appear in the material as written",
                        action_required=False,
                        assigned_to="orchestrator",
                    )
                ],
                change_required=True,
                change_rationale="one accepted critical issue requires an edit",
            )
        ],
        "controlled_editor": [
            apply_edit(
                path="sample_manuscript.md",
                old_text=UNSUPPORTED,
                new_text=HEDGED,
                issue_ids=["evidence_auditor:ev-1"],
                rationale="mark the figure provisional instead of presenting it as established",
            ),
            edit_result(
                "sample_manuscript.md",
                changes=[
                    change(
                        "c1",
                        issue_ids=["evidence_auditor:ev-1"],
                        before=UNSUPPORTED,
                        after=HEDGED,
                        rationale="smallest edit that stops an uncited number reading as established",
                    )
                ],
            ),
        ],
        "diff_reviewer": [diff_review(approved=True, notes=["the hunk is confined to the abstract sentence"])],
        "adversarial_reviewer": [
            verdict(
                passed=True,
                minor=[finding("the figure is still uncited, but it is now declared as uncited", location="abstract")],
                rationale="tried to find fabrication, removed caveats and scope inflation; the edit only weakens a claim",
            )
        ],
        "verifier": [verification()],
        "final_reporter": [
            final_report(
                summary="one critical unsupported claim was fixed and verified; one reviewer claim was rejected as unsupported",
                accepted=["evidence_auditor:ev-1 (critical): the figure is now marked preliminary and uncited"],
                rejected=["technical_reviewer:tech-1 rejected: the quoted sentence is not in the material"],
                changes=["abstract: added the provisional/uncited qualifier"],
                verification_summary=["all mandatory checks passed, including the semantic checks"],
                claimed_status="succeeded",
            )
        ],
    }


async def run_scripted_demo(objective: str) -> int:
    workdir = ROOT / "examples" / "_run"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    target = workdir / "sample_manuscript.md"
    shutil.copy(MATERIAL, target)

    settings = Settings(
        workspace_root=workdir,
        allowed_write_roots=[workdir],
        ledger_path=workdir / "artifacts" / "audit.sqlite3",
        models=ModelsByRole(),
        budget=Budget(max_revision_rounds=2, agent_timeout_s=30.0),
        tracing=TracingSettings(enabled=False),
    ).resolved()

    model = ScriptedModel(scripted_payloads())
    ledger = open_ledger(settings.ledger_path)
    workflow = ReviewWorkflow(
        settings,
        ledger=ledger,
        registry=build_default_registry(),
        model_provider=ScriptedModelProvider(model),
    )
    print("mode:      SCRIPTED (canned model responses, real workflow code, no API calls)")
    print(f"workspace: {workdir}\n")
    try:
        result = await workflow.run(objective=objective, target=target)
        events = ledger.events(result.workflow_id)
        spans = ledger.spans(result.workflow_id)
        edited_text = await asyncio.to_thread(target.read_text, encoding="utf-8")
    finally:
        ledger.close()
    print_result(result, edited_text)
    print_ledger(events, spans)
    print(f"audit ledger: {settings.ledger_path}")
    print(f"inspect it:   reviewflow audit --workflow-id {result.workflow_id}")
    return 0 if result.ok else 1


async def run_live(objective: str, target: Path) -> int:
    from reviewflow.providers.routing import ModelRouter

    settings = Settings.from_env()
    router = ModelRouter.from_settings(settings)
    if router is None:
        print(
            "--live needs a model provider credential. Expected a ChatGPT OAuth login "
            "(~/.codex/auth.json or Hermes' auth.json), a commandcode login "
            "(~/.commandcode/auth.json), or OPENAI_API_KEY. Run `reviewflow providers`.",
            file=sys.stderr,
        )
        return 2
    target = await asyncio.to_thread(lambda: target.expanduser().resolve())
    settings = settings.model_copy(
        update={"workspace_root": target.parent, "allowed_write_roots": [target.parent]}
    ).resolved()
    ledger = open_ledger(settings.ledger_path)
    workflow = ReviewWorkflow(settings, ledger=ledger, registry=build_default_registry())
    print(f"mode: LIVE (real model calls)\nprovider: {router.default_provider}\ntarget: {target}\n")
    try:
        result = await workflow.run(objective=objective, target=target)
    finally:
        ledger.close()
    return 0 if result.ok else 1


def print_result(result, edited_text: str) -> None:
    print(f"status:   {result.status.value}")
    print(f"reason:   {result.status_reason}")
    print(f"stages:   {' -> '.join(stage.value for stage in result.stages_completed)}")
    print(f"rounds:   {result.rounds_executed}")
    print(f"coverage: expected={result.review_coverage.expected} succeeded={result.review_coverage.succeeded} "
          f"failed={result.review_coverage.failed}")
    print(f"usage:    {result.usage.model_calls} model calls, {result.usage.total_tokens} tokens, "
          f"{result.usage.wall_clock_ms} ms")
    for label, results in (
        ("passed", [r.check for r in result.verification_results if r.passed]),
        ("failed", [r.check for r in result.verification_results if not r.passed]),
    ):
        print(f"checks {label}: {', '.join(results) if results else '-'}")
    if result.unresolved_issues:
        print("unresolved:")
        for item in result.unresolved_issues:
            print(f"  - [{item.severity.value}] {item.issue_id}: {item.summary}")
    if result.final_output is not None:
        print("\nfinal report (orchestrator-authored):")
        print(f"  {result.final_output.executive_summary}")
        for line in result.final_output.changes_applied:
            print(f"  changed: {line}")
        for disclosure in result.final_output.disclosures:
            print(f"  ! {disclosure}")
    print("\nedited file, first 8 lines:")
    for line in edited_text.splitlines()[:8]:
        print(f"  {line}")


def print_ledger(events, spans) -> None:
    print(f"\naudit ledger: {len(events)} events, {len(spans)} spans")
    for event in events:
        if event.event_type.value in {"stage_start", "transition", "verification", "failure", "run_finalized"}:
            label = f"  {event.seq:>3} {event.event_type.value:<12}"
            if event.transition_from:
                label += f" {event.transition_from.value} -> {event.transition_to.value}"
            elif event.verification_status:
                label += f" {event.verification_status}"
            elif event.stage:
                label += f" {event.stage.value}"
            print(label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call the real OpenAI API instead of scripted responses")
    parser.add_argument("--target", help="artifact to review (--live only)")
    parser.add_argument(
        "--objective",
        default="Find unsupported claims and incoherence, and fix the smallest defensible set of issues.",
    )
    args = parser.parse_args()
    configure_logging()
    if args.live:
        target = Path(args.target) if args.target else MATERIAL
        return asyncio.run(run_live(args.objective, target))
    return asyncio.run(run_scripted_demo(args.objective))


if __name__ == "__main__":
    raise SystemExit(main())
