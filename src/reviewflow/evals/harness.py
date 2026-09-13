"""Evaluation harness.

Runs every scenario against a real workflow with a scripted model, checks the
declared pass criteria, and reports a machine-readable table. Deterministic and
free: no API calls are made, so it is safe to run in CI and safe to use as a
regression gate for prompt changes.

Regression-testing prompts: because the criteria are behavioural (status, ledger
events, coverage, artifact contents) rather than textual, a prompt change is
accepted when the criteria still hold. Scenarios that assert *communication*
properties (for example that a rejection is disclosed) are what stop a prompt
"improvement" from silently dropping an honest disclosure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from reviewflow.evals.scenarios import Scenario, build_scenarios
from reviewflow.models.enums import WorkflowStatus
from reviewflow.testkit import DEFAULT_MATERIAL, ScriptedRun, run_scripted


@dataclass
class EvalResult:
    """Outcome of one scenario."""

    name: str
    passed: bool
    expected_status: WorkflowStatus
    observed_status: WorkflowStatus | None
    failures: list[str] = field(default_factory=list)
    status_reason: str = ""
    rounds: int = 0
    model_calls: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    run: ScriptedRun | None = None

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return (
            f"[{mark}] {self.name:<38} status={self.observed_status.value if self.observed_status else 'n/a':<10} "
            f"rounds={self.rounds} calls={self.model_calls} tokens={self.total_tokens} {self.duration_ms}ms"
        )


async def run_scenario(scenario: Scenario) -> EvalResult:
    """Execute one scenario and evaluate its criteria."""
    started = time.monotonic()
    workspace_override = scenario.workspace
    try:
        run = await run_scripted(
            scenario.scripts(),
            material=scenario.material or DEFAULT_MATERIAL,
            workspace=workspace_override,
            max_revision_rounds=scenario.max_revision_rounds,
            verification_config=scenario.verification_config,
        )
    except Exception as exc:
        return EvalResult(
            name=scenario.name,
            passed=False,
            expected_status=scenario.expected_status,
            observed_status=None,
            failures=[f"scenario raised {type(exc).__name__}: {exc}"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    failures = list(scenario.assertion(run))
    if run.result.status is not scenario.expected_status:
        failures.insert(
            0, f"status {run.result.status.value} != expected {scenario.expected_status.value}"
        )
    try:
        run.model.assert_exhausted()
    except AssertionError as exc:
        failures.append(str(exc))

    return EvalResult(
        name=scenario.name,
        passed=not failures,
        expected_status=scenario.expected_status,
        observed_status=run.result.status,
        failures=failures,
        status_reason=run.result.status_reason,
        rounds=run.result.rounds_executed,
        model_calls=run.result.usage.model_calls,
        total_tokens=run.result.usage.total_tokens,
        duration_ms=int((time.monotonic() - started) * 1000),
        run=run,
    )


async def run_evals(*, only: list[str] | None = None, keep_workspaces: bool = False) -> list[EvalResult]:
    """Run all (or selected) scenarios."""
    scenarios = build_scenarios()
    if only:
        scenarios = [s for s in scenarios if s.name in set(only)]
    results: list[EvalResult] = []
    for scenario in scenarios:
        result = await run_scenario(scenario)
        results.append(result)
        if not keep_workspaces and result.run is not None:
            result.run.workspace.cleanup()
    return results


def format_report(results: list[EvalResult]) -> str:
    """Human-readable report with the failing criteria spelled out."""
    lines = ["", "=" * 96, "reviewflow evaluation", "=" * 96]
    for result in results:
        lines.append(result.line())
        for failure in result.failures:
            lines.append(f"        - {failure}")
    passed = sum(1 for r in results if r.passed)
    lines.append("-" * 96)
    lines.append(f"{passed}/{len(results)} scenarios passed")
    lines.append("=" * 96)
    return "\n".join(lines)
