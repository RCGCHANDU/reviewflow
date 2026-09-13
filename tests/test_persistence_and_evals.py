"""Persistence, observability and evaluation.

The audit ledger is the workflow's database, so it gets tested as one: sequence
numbers, round trips through every backend, the terminal record, and the artifacts
written next to it. The evaluation scenarios are also run here, so a behavioural
regression fails the normal test suite rather than only the eval command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reviewflow.evals import build_scenarios, format_report, run_evals, run_scenario
from reviewflow.observability import LedgerTraceProcessor, redact
from reviewflow.persistence import AuditEvent, TraceSpanRecord, WorkflowRunRecord, open_ledger
from reviewflow.persistence.jsonl_ledger import JsonlLedger
from reviewflow.persistence.ledger import Ledger
from reviewflow.persistence.memory import InMemoryLedger
from reviewflow.persistence.records import EventType as RecordsEventType
from reviewflow.testkit import run_scripted
from test_orchestration import four_reviewer_scripts

# --------------------------------------------------------------------------- #
# Ledger backends
# --------------------------------------------------------------------------- #


def sample_event(workflow_id: str = "wf-x") -> AuditEvent:
    return AuditEvent(
        workflow_id=workflow_id,
        event_type=RecordsEventType.STAGE_END,
        stage="plan",
        agent_role="orchestrator",
        outcome="ok",
        issue_ids=["ev-1"],
        detail={"units": 2},
    )


def test_factory_infers_the_backend_from_the_suffix(tmp_path) -> None:
    assert isinstance(open_ledger(tmp_path / "a.sqlite3"), object)
    assert type(open_ledger(tmp_path / "a.sqlite3")).__name__ == "SqliteLedger"
    assert type(open_ledger(tmp_path / "a.jsonl")).__name__ == "JsonlLedger"
    assert type(open_ledger(None)).__name__ == "InMemoryLedger"


def test_sequence_numbers_are_monotonic_per_workflow() -> None:
    ledger = InMemoryLedger()
    first = ledger.record(sample_event("wf-a"))
    second = ledger.record(sample_event("wf-a"))
    other = ledger.record(sample_event("wf-b"))
    assert (first.seq, second.seq, other.seq) == (1, 2, 1)


def test_sqlite_ledger_round_trip(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "audit.sqlite3")
    ledger.record(sample_event())
    ledger.record_span(
        TraceSpanRecord(
            workflow_id="wf-x",
            trace_id="trace_1",
            span_id="span_1",
            span_type="generation",
            name="gpt",
            duration_ms=12,
            detail={"model": "gpt-5.4-mini"},
        )
    )
    ledger.finalize(
        WorkflowRunRecord(
            workflow_id="wf-x",
            task_id="t1",
            target="manuscript.md",
            objective="o",
            status="succeeded",
            status_reason="all gates passed",
            rounds_executed=1,
            stages_completed=["ingest", "complete"],
            total_tokens=180,
            model_calls=2,
            failed_roles={},
            result_json={"status": "succeeded"},
        )
    )
    events = ledger.events("wf-x")
    assert len(events) == 1 and events[0].issue_ids == ["ev-1"] and events[0].stage == "plan"
    spans = ledger.spans("wf-x")
    assert spans[0].span_type == "generation" and spans[0].duration_ms == 12
    record = ledger.run("wf-x")
    assert record is not None and record.status == "succeeded" and record.total_tokens == 180
    ledger.close()


def test_jsonl_ledger_round_trip(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    ledger = open_ledger(path)
    ledger.record(sample_event())
    ledger.finalize(
        WorkflowRunRecord(
            workflow_id="wf-x",
            task_id="t1",
            target="manuscript.md",
            objective="o",
            status="blocked",
            status_reason="why",
            rounds_executed=2,
            stages_completed=["ingest"],
            total_tokens=1,
            model_calls=1,
        )
    )
    events_file = path.with_suffix(path.suffix + ".events.jsonl")
    assert events_file.exists()
    lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["workflow_id"] == "wf-x" and lines[0]["seq"] == 1
    assert ledger.run("wf-x").status == "blocked"


def test_ledger_is_isolated_behind_an_interface() -> None:
    """Swapping backends must not require touching the engine."""
    from reviewflow.persistence.ledger import Ledger

    for ledger in (InMemoryLedger(), open_ledger(None)):
        assert isinstance(ledger, Ledger)
        for method in ("record", "record_span", "finalize", "events", "spans", "run", "close"):
            assert callable(getattr(ledger, method))


# --------------------------------------------------------------------------- #
# The workflow writes to the ledger and to artifacts
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_persists_the_run_and_its_artifacts(make_workspace) -> None:
    ws = make_workspace()
    run = await run_scripted(four_reviewer_scripts(), workspace=ws)
    assert run.result.status.value == "succeeded"

    kinds = [event.event_type.value for event in run.events]
    for expected in ("stage_start", "transition", "agent_call", "verification", "run_finalized"):
        assert expected in kinds, f"{expected} missing from the audit ledger"

    transitions = [(e.transition_from.value, e.transition_to.value) for e in run.events if e.transition_from]
    assert ("synthesize", "controlled_edit") in transitions
    assert ("verify", "final_review") in transitions

    artifact = Path(run.result.artifacts["audit_events"])
    # Reading a small artifact inside a test: blocking I/O here is deliberate.
    assert artifact.exists()  # noqa: ASYNC240
    persisted = json.loads(artifact.read_text(encoding="utf-8"))  # noqa: ASYNC240
    assert len(persisted) == len(run.events)
    assert persisted[0]["workflow_id"] == run.result.workflow_id


@pytest.mark.asyncio
async def test_ledger_records_retries_and_outcomes(make_workspace) -> None:
    from reviewflow.simulate import text

    scripts = four_reviewer_scripts()
    scripts["evidence_auditor"] = [text("not json"), text("still not json")]
    scripts["synthesizer"] = [__import__("reviewflow.fixtures", fromlist=["issue_map"]).issue_map(accepted=[], rejected=[], change_required=False)]
    for key in ("controlled_editor", "diff_reviewer", "verifier"):
        scripts.pop(key, None)
    run = await run_scripted(scripts, workspace=make_workspace())
    events = [e for e in run.events if e.agent_role == "evidence_auditor"]
    assert events and events[0].retry_count == 1 and events[0].outcome.value == "failed"
    failures = [e for e in run.events if e.event_type.value == "failure"]
    kinds = [e.detail.get("failure_kind") for e in failures]
    assert "invalid_output" in kinds, f"the failure class must be recorded, saw {kinds}"


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #


def test_redact_masks_credentials() -> None:
    text = "key sk-abc123456789 and bearer TOKENVALUE12345 plus AKIAIOSFODNN7EXAMPLE"
    masked = redact(text)
    assert "sk-abc123456789" not in masked and "TOKENVALUE12345" not in masked
    assert "AKIAIOSFODNN7EXAMPLE" not in masked
    assert masked.count("[REDACTED]") == 3


@pytest.mark.asyncio
async def test_sdk_spans_are_mirrored_into_the_ledger_without_content(make_workspace) -> None:
    """Tracing on: spans land in the ledger and carry no document text."""
    from agents.tracing import set_trace_processors

    ws = make_workspace()
    settings = ws.settings.model_copy(update={"tracing": ws.settings.tracing.model_copy(update={"enabled": True})})
    ledger = InMemoryLedger()
    processor = LedgerTraceProcessor(ledger, default_workflow_id="wf-trace")
    set_trace_processors([processor])
    try:
        from reviewflow.agents import build_default_registry
        from reviewflow.orchestration.workflow import ReviewWorkflow
        from reviewflow.simulate import ScriptedModel, ScriptedModelProvider

        model = ScriptedModel(four_reviewer_scripts())
        workflow = ReviewWorkflow(
            settings, ledger=ledger, registry=build_default_registry(), model_provider=ScriptedModelProvider(model)
        )
        result = await workflow.run(objective="trace this", target=ws.target)
        assert result.status.value == "succeeded"
        spans = ledger.spans(result.workflow_id)
        assert spans, "spans should be mirrored into the ledger"
        span_types = {span.span_type for span in spans}
        assert "agent" in span_types or "generation" in span_types
        blob = json.dumps([span.model_dump(mode="json") for span in spans])
        assert "97.3% answer accuracy" not in blob, "span payloads must not copy the document"
    finally:
        set_trace_processors([])


# --------------------------------------------------------------------------- #
# Evaluation scenarios, as tests
# --------------------------------------------------------------------------- #

SCENARIOS = build_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
@pytest.mark.asyncio
async def test_eval_scenario_passes(scenario) -> None:
    """Every eval scenario is also a test, so a behavioural regression fails CI."""
    result = await run_scenario(scenario)
    if result.run is not None:
        result.run.workspace.cleanup()
    assert result.passed, format_report([result])


@pytest.mark.asyncio
async def test_eval_report_marks_failures_explicitly() -> None:
    results = await run_evals(only=["clean_input_no_edit"])
    assert len(results) == 1 and results[0].passed
    report = format_report(results)
    assert "[PASS] clean_input_no_edit" in report and "1/1 scenarios passed" in report


# --------------------------------------------------------------------------- #
# Real API integration (opt-in only)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or os.environ.get("REVIEWFLOW_LIVE_TEST") != "1",
    reason="live integration test: set OPENAI_API_KEY and REVIEWFLOW_LIVE_TEST=1 to run",
)
@pytest.mark.asyncio
async def test_live_minimal_run(make_workspace) -> None:  # pragma: no cover - costs credits
    """One real end-to-end run with a single reviewer.

    Gated twice on purpose: the key must exist *and* REVIEWFLOW_LIVE_TEST=1 must be
    set, so a normal `pytest` run can never spend API credits.
    """
    from reviewflow.config import Budget, ModelsByRole, Settings, TracingSettings
    from reviewflow.testkit import default_settings

    ws = make_workspace()
    settings: Settings = default_settings(ws.root, max_revision_rounds=1, timeout_s=120.0).model_copy(
        update={
            "models": ModelsByRole(),
            "budget": Budget(max_revision_rounds=1, max_turns_per_agent=4, agent_timeout_s=120.0),
            "tracing": TracingSettings(enabled=False),
        }
    )
    scripts = {
        "scope_guardrail": [__import__("reviewflow.simulate", fromlist=["scope_allow"]).scope_allow()],
        "planner": [__import__("reviewflow.fixtures", fromlist=["plan"]).plan()],
        "evidence_auditor": [
            __import__("reviewflow.fixtures", fromlist=["report"]).report("evidence_auditor")
        ],
        "structure_reviewer": [
            __import__("reviewflow.fixtures", fromlist=["report"]).report("structure_reviewer")
        ],
        "synthesizer": [
            __import__("reviewflow.fixtures", fromlist=["issue_map"]).issue_map(
                accepted=[], rejected=[], change_required=False
            )
        ],
        "adversarial_reviewer": [
            __import__("reviewflow.fixtures", fromlist=["verdict"]).verdict(passed=True)
        ],
        "final_reporter": [
            __import__("reviewflow.fixtures", fromlist=["final_report"]).final_report(claimed_status="succeeded")
        ],
    }
    run = await run_scripted(scripts, workspace=ws, settings=settings)
    assert run.result.status.value in {"succeeded", "degraded", "blocked"}
    assert run.result.usage.model_calls > 0


def test_importing_the_ledger_module_does_not_write_to_disk(tmp_path: Path) -> None:
    """Importing a module must not create files.

    Regression test: the module ended with an import-time
    `assert isinstance(JsonlLedger(Path("unused.jsonl")), Ledger)`. Constructing the ledger
    creates its files, so merely importing it littered the current working directory with
    empty `unused.jsonl.{events,runs,spans}.jsonl` files - which then showed up as stray
    untracked files in the repository.
    """
    script = "import reviewflow.persistence.jsonl_ledger"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == [], f"import wrote to the working directory: {list(tmp_path.iterdir())}"


def test_jsonl_ledger_round_trips_in_an_explicit_directory(tmp_path: Path) -> None:
    """The ledger still behaves when it is used deliberately (the conformance check changed)."""
    ledger = JsonlLedger(tmp_path / "ledger.jsonl")
    assert isinstance(ledger, Ledger)
    assert list((tmp_path).iterdir()), "constructing a ledger on purpose may create its files"
