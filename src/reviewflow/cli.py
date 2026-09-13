"""Command line interface.

Commands are explicit about which model backend they use:

* ``run`` executes a real review and therefore requires a real API key. It refuses
  to start without one rather than silently substituting a scripted model.
* ``demo`` runs the scripted end-to-end example: real workflow code, scripted
  model, no API key, no cost. It is labelled as such in its output.
* ``evals`` runs the evaluation scenarios (scripted by default, ``--live`` for the
  real API).
* ``audit`` reads the audit ledger back: events, spans and the terminal record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from reviewflow.agents import build_default_registry
from reviewflow.config import Settings
from reviewflow.errors import ReviewflowError
from reviewflow.models.contracts import WorkflowResult
from reviewflow.observability import configure_logging, get_logger, install_ledger_trace_processor
from reviewflow.orchestration.workflow import ReviewWorkflow
from reviewflow.persistence import open_ledger
from reviewflow.simulate import ScriptedModelProvider
from reviewflow.testkit import DEFAULT_MATERIAL, Workspace
from reviewflow.verification.checks import CHECKS

_log = get_logger("cli")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
EXIT_MISUSE = 3


def _require_credentials() -> str:
    """Refuse to start a real run unless some provider can actually serve it.

    A run makes real model calls, so it must not silently fall back to a scripted
    model, and it must not fail deep inside the first agent either: discover any
    usable credential up front (an OpenAI ChatGPT OAuth login, a commandcode login,
    or a plain API key) and report what was found.
    """
    from reviewflow.providers.routing import ModelRouter

    settings = Settings.from_env()
    router = ModelRouter.from_settings(settings)
    if router is None:
        raise ReviewflowError(
            "no model provider credential found. Expected one of:\n"
            "  - OpenAI ChatGPT OAuth: sign in with `codex login`, or `hermes auth add openai-codex`\n"
            "  - commandcode: log in with the commandcode CLI so ~/.commandcode/auth.json exists\n"
            "  - a plain API key in OPENAI_API_KEY\n"
            "Run `reviewflow providers` to see what was discovered. "
            "`reviewflow demo` needs no credential (scripted model)."
        )
    return router.default_provider


def cmd_providers(args: argparse.Namespace) -> int:
    """Show which credentials were discovered, from where, and what each can serve."""
    from reviewflow.providers.routing import ModelRouter, credential_status

    status = credential_status()
    settings = Settings.from_env()
    router = ModelRouter.from_settings(settings)
    if args.json:
        print(json.dumps({"credentials": status, "routing": router.describe() if router else None}, indent=2, default=str))
        return EXIT_OK

    print(f"requested provider: {settings.provider}")
    print(f"active provider:    {router.default_provider if router else '(none - no credential found)'}")
    print()
    for name, info in status.items():
        mark = "OK  " if info.get("available") else "--  "
        print(f"[{mark}] {name}  api_mode={info.get('api_mode')}  streaming_required={info.get('requires_streaming')}")
        if info.get("available"):
            print(f"         source: {info.get('source')}")
            for key in ("user", "masked_token", "masked_key", "seconds_left", "refreshable", "base_url"):
                if info.get(key) not in (None, ""):
                    print(f"         {key}: {info[key]}")
        else:
            print("         not signed in")
    if router is not None:
        print("\nrouting (role defaults per credentialed provider):")
        for name, route in router.describe()["routes"].items():
            print(f"  {name}: {route['api_mode']} @ {route['base_url']}")
            models = route["models"]
            print(f"    orchestrator={models['orchestrator']} reviewer={models['reviewer']} editor={models['editor']}")
            print(f"    adversarial={models['adversarial']} cheap={models['cheap']} verifier={models['verifier']}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Live-check each credentialed provider: plain call, structured output, tool loop."""
    import asyncio as _asyncio

    from reviewflow.providers.doctor import run_doctor

    configure_logging(json_lines=not args.plain_logs)
    settings = Settings.from_env()
    report = _asyncio.run(run_doctor(settings, provider=args.provider, model=args.model))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"doctor: requested={report['requested_provider']} active={report['active_provider']}")
        for check in report["checks"]:
            mark = "OK  " if check["ok"] else "FAIL"
            print(f"  [{mark}] {check['model']} -- {check['check']}")
            if check.get("detail"):
                print(f"         {check['detail']}")
        print(f"\n{report['passed']}/{report['total']} checks passed")
    return EXIT_OK if report["passed"] == report["total"] else EXIT_FAILED


def cmd_run(args: argparse.Namespace) -> int:
    active = _require_credentials()
    settings = Settings.from_env()
    if args.workspace:
        settings = settings.model_copy(update={"workspace_root": Path(args.workspace)})
    if args.rounds is not None:
        settings = settings.model_copy(
            update={"budget": settings.budget.model_copy(update={"max_revision_rounds": args.rounds})}
        )
    settings = settings.resolved()
    configure_logging(json_lines=not args.plain_logs)
    print(f"provider: {active}")

    ledger = open_ledger(settings.ledger_path)
    if settings.tracing.enabled and settings.tracing.to_ledger:
        # Replace the SDK's default trace exporter with the ledger processor. A run
        # authenticated through an OAuth provider holds no OpenAI-platform key, so the
        # default exporter could only fail on every span; and trace payloads leaving the
        # host is not something this tool should do implicitly.
        install_ledger_trace_processor(ledger)
    workflow = ReviewWorkflow(settings, ledger=ledger, registry=build_default_registry())
    try:
        result = asyncio.run(
            workflow.run(objective=args.objective, target=Path(args.target), constraints=args.constraint or None)
        )
    finally:
        ledger.close()
    return _print_result(result, as_json=args.json)


def _print_result(result: WorkflowResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    else:
        print(f"workflow:  {result.workflow_id}")
        print(f"status:    {result.status.value}")
        print(f"reason:    {result.status_reason}")
        print(f"stages:    {' -> '.join(stage.value for stage in result.stages_completed)}")
        print(f"rounds:    {result.rounds_executed}")
        print(f"coverage:  expected={result.review_coverage.expected} failed={result.review_coverage.failed}")
        print(f"usage:     {result.usage.model_calls} calls, {result.usage.total_tokens} tokens, "
              f"{result.usage.wall_clock_ms} ms")
        for label, results in (
            ("passed", [r.check for r in result.verification_results if r.passed]),
            ("failed", [r.check for r in result.verification_results if not r.passed]),
        ):
            if results:
                print(f"checks {label}: {', '.join(results)}")
        if result.unresolved_issues:
            print("unresolved:")
            for item in result.unresolved_issues:
                print(f"  - [{item.severity.value}] {item.issue_id}: {item.summary}")
        if result.final_output is not None:
            print("\nfinal report:")
            print(result.final_output.executive_summary)
            for disclosure in result.final_output.disclosures:
                print(f"  ! {disclosure}")
        if result.artifacts:
            print("artifacts: " + ", ".join(f"{k}={v}" for k, v in result.artifacts.items()))

    match result.status.value:
        case "succeeded":
            return EXIT_OK
        case "degraded":
            return EXIT_FAILED
        case "blocked":
            return EXIT_BLOCKED
        case _:
            return EXIT_FAILED


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_demo(args: argparse.Namespace) -> int:
    """Scripted end-to-end example: real workflow, scripted model, no API key."""
    from reviewflow.evals.scenarios import SCENARIO_END_TO_END
    from reviewflow.persistence.memory import InMemoryLedger
    from reviewflow.simulate import ScriptedModel

    configure_logging(json_lines=not args.plain_logs)
    workspace = Workspace.build(SCENARIO_END_TO_END.material or DEFAULT_MATERIAL)
    model = ScriptedModel(SCENARIO_END_TO_END.scripts())
    print("NOTE: this is the scripted demo - model responses are canned and no API calls are made.")
    print(f"workspace: {workspace.root}\n")

    workflow = ReviewWorkflow(
        workspace.settings,
        ledger=InMemoryLedger(),
        registry=build_default_registry(),
        model_provider=ScriptedModelProvider(model),
    )
    result = asyncio.run(workflow.run(objective=args.objective, target=workspace.target))
    code = _print_result(result, as_json=args.json)
    print(f"\nedited file:\n{workspace.text()}")
    return code


def cmd_evals(args: argparse.Namespace) -> int:
    from reviewflow.evals import format_report, run_evals

    if args.live:
        _require_credentials()
    results = asyncio.run(run_evals(only=args.only, keep_workspaces=args.keep))
    print(format_report(results))
    return EXIT_OK if all(r.passed for r in results) else EXIT_FAILED


def cmd_audit(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    ledger = open_ledger(settings.ledger_path)
    try:
        if not args.workflow_id:
            raise ReviewflowError("--workflow-id is required")
        events = ledger.events(args.workflow_id)
        if not events:
            print(f"no events for {args.workflow_id} in {settings.ledger_path}")
            return EXIT_FAILED
        if args.json:
            record = ledger.run(args.workflow_id)
            print(
                json.dumps(
                    {
                        "events": [event.model_dump(mode="json") for event in events],
                        "spans": [span.model_dump(mode="json") for span in ledger.spans(args.workflow_id)],
                        "run": record.model_dump(mode="json") if record is not None else None,
                    },
                    indent=2,
                    default=str,
                )
            )
            return EXIT_OK
        for event in events:
            label = f"{event.seq:>4} {event.stage.value if event.stage else '-':<18} {event.event_type.value:<14}"
            extra = []
            if event.agent_role:
                extra.append(event.agent_role)
            if event.outcome:
                extra.append(event.outcome.value)
            if event.latency_ms is not None:
                extra.append(f"{event.latency_ms}ms")
            if event.verification_status:
                extra.append(event.verification_status)
            print(f"{label} {' '.join(extra)}")
        record = ledger.run(args.workflow_id)
        if record is not None:
            print(f"\nterminal: {record.status} ({record.status_reason}) rounds={record.rounds_executed} "
                  f"tokens={record.total_tokens}")
        return EXIT_OK
    finally:
        ledger.close()


def cmd_checks(args: argparse.Namespace) -> int:
    print("registered verification checks:")
    for name, spec in sorted(CHECKS.items()):
        print(f"  {name:<34} mandatory={spec.mandatory}  {spec.description}")
    from reviewflow.verification.runner import SEMANTIC_CHECKS

    print("\nsemantic checks (verifier agent):")
    for name, instruction in sorted(SEMANTIC_CHECKS.items()):
        print(f"  {name:<34} {instruction[:80]}...")
    return EXIT_OK


def cmd_targets(args: argparse.Namespace) -> int:
    """List the role -> model mapping and permission policy in force."""
    settings = Settings.from_env()
    registry = build_default_registry()
    print(f"workspace root:      {settings.workspace_root}")
    print(f"allowed write roots: {[str(p) for p in settings.allowed_write_roots]}")
    print(f"ledger:              {settings.ledger_path}")
    print(f"revision rounds:     {settings.budget.max_revision_rounds}")
    print(f"agent timeout:       {settings.budget.agent_timeout_s}s")
    from reviewflow.agents.factory import AgentFactory
    from reviewflow.providers.routing import ModelRouter

    router = ModelRouter.from_settings(settings)
    # The factory is the single source of truth for role -> model, so the table shows
    # what a run would actually build.
    factory = AgentFactory(settings, registry, router=router)
    print(f"provider:            {router.default_provider if router else '(none discovered)'}")
    print("\nroles:")
    for key in sorted(registry.policies.policies):
        spec = registry.specs.get(key)
        policy = registry.policies.get(key)
        tools = sorted(policy.allowed_tools)
        print(
            f"  {key:<22} mutating={policy.may_mutate!s:<5} "
            f"model={factory.model_for(key):<24} tools={tools}"
        )
        if spec is None:
            print("       (internal role)")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reviewflow",
        description="Deterministic multi-agent review -> edit -> adversarial review -> verification workflow.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a real review (uses a discovered OAuth login or API key)")
    run.add_argument("--target", required=True, help="path to the artifact to review")
    run.add_argument("--objective", required=True, help="what the review must achieve")
    run.add_argument("--workspace", help="workspace root (defaults to REVIEWFLOW_WORKSPACE_ROOT)")
    run.add_argument("--rounds", type=int, help="maximum revision rounds")
    run.add_argument("--constraint", action="append", help="immutable constraint (repeatable)")
    run.add_argument("--json", action="store_true", help="emit the full WorkflowResult as JSON")
    run.add_argument("--plain-logs", action="store_true", help="human-readable logs instead of JSON lines")
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="scripted end-to-end demo (no API key, no cost)")
    demo.add_argument("--objective", default="Find unsupported or incoherent claims and fix the smallest defensible set.")
    demo.add_argument("--json", action="store_true")
    demo.add_argument("--plain-logs", action="store_true")
    demo.set_defaults(func=cmd_demo)

    evals = sub.add_parser("evals", help="run the evaluation scenarios")
    evals.add_argument("--only", action="append", help="scenario name (repeatable)")
    evals.add_argument("--live", action="store_true", help="use the real API instead of the scripted model")
    evals.add_argument("--keep", action="store_true", help="keep scenario workspaces for inspection")
    evals.set_defaults(func=cmd_evals)

    audit = sub.add_parser("audit", help="read the audit ledger for a workflow")
    audit.add_argument("--workflow-id", required=True)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    checks = sub.add_parser("checks", help="list the registered verification checks")
    checks.set_defaults(func=cmd_checks)

    providers = sub.add_parser("providers", help="show discovered OAuth/API credentials and routing")
    providers.add_argument("--json", action="store_true")
    providers.set_defaults(func=cmd_providers)

    doctor = sub.add_parser("doctor", help="live-check each credentialed provider end to end")
    doctor.add_argument("--provider", help="check only this provider (codex, commandcode, openai)")
    doctor.add_argument("--model", help="check only this model")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--plain-logs", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    targets = sub.add_parser("roles", help="show the role/model/tool policy table")
    targets.set_defaults(func=cmd_targets)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ReviewflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MISUSE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
