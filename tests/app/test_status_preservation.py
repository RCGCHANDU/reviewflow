"""The application must never rewrite an uncomfortable terminal state.

This is the application-level counterpart to the engine's own eval suite: each scenario is
run *through the API* and its engine-computed status is compared with what the application
reports. If the app ever mapped a failed verification to success, or hid a loss of reviewer
coverage, these tests fail.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from reviewflow.evals.scenarios import build_scenarios

from .conftest import start_run, wait_for_terminal

#: Scenarios that exercise the interesting terminal states, with the app's revision budget
#: set to the scenario's own (they are authored against a specific number of rounds).
SCENARIOS = {scenario.name: scenario for scenario in build_scenarios()}

CASES = [
    ("successful_end_to_end", "succeeded"),
    ("clean_input_no_edit", "succeeded"),
    ("unsupported_claim_detected_and_fixed", "succeeded"),
    ("reviewer_hallucination_rejected", "succeeded"),
    ("failed_verification_blocks_success", "blocked"),
    ("conflicting_reviewers", "blocked"),
    ("reviewer_timeout_degrades", "degraded"),
]


@pytest.mark.parametrize(("scenario_name", "expected_status"), CASES)
def test_engine_terminal_status_survives_the_application(
    client: TestClient, project: dict[str, Any], source: dict[str, Any], scenario_name: str, expected_status: str
) -> None:
    scenario = SCENARIOS[scenario_name]
    assert scenario.expected_status.value == expected_status, "the engine's own expectation moved"

    # The uploaded sample is the scenario's own material, so the run is deterministic.
    loaded = client.post(
        f"/api/v1/projects/{project['id']}/samples", params={"sample_id": f"scenario:{scenario_name}"}
    ).json()

    run = start_run(
        client,
        project_id=project["id"],
        source_id=loaded["id"],
        scenario=scenario_name,
        rounds=scenario.max_revision_rounds,
    )
    detail = wait_for_terminal(client, run["id"], timeout=120)

    assert detail["run"]["status"] == expected_status, (
        f"{scenario_name}: the application reported {detail['run']['status']!r} "
        f"where the engine produced {expected_status!r}"
    )

    inspection = client.get(f"/api/v1/workflows/{run['id']}/inspection").json()
    assert inspection["terminal"]["status"] == expected_status
    # The explanation is derived from recorded facts, and names the outcome.
    assert inspection["status_explanation"]
    if expected_status == "blocked":
        assert "refused to pass" in inspection["status_explanation"] or "unresolved" in inspection["status_explanation"]
    if expected_status == "degraded":
        assert "degraded" in inspection["status_explanation"] or "coverage" in inspection["status_explanation"]


def test_a_failing_mandatory_check_is_visible_and_never_green(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """A blocked run must expose the specific failing check, and must not read as success."""
    loaded = client.post(
        f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:failed_verification_blocks_success"}
    ).json()
    run = start_run(
        client,
        project_id=project["id"],
        source_id=loaded["id"],
        scenario="failed_verification_blocks_success",
        rounds=1,
    )
    wait_for_terminal(client, run["id"], timeout=120)
    inspection = client.get(f"/api/v1/workflows/{run['id']}/inspection").json()

    assert inspection["run"]["status"] == "blocked"
    results = inspection["verification"]["results"]
    failing = [result["check"] for result in results if not result["passed"]]
    assert failing, "a blocked run must have at least one failing mandatory check"
    # The failure is reported with evidence, not as a generic error.
    failing_result = next(result for result in results if not result["passed"])
    assert failing_result.get("error") or failing_result.get("evidence")

    unresolved = inspection["unresolved_issues"]
    assert unresolved, "a blocked run must list the issues that blocked it"


def test_degraded_is_not_reported_as_succeeded(
    client: TestClient, project: dict[str, Any], source: dict[str, Any]
) -> None:
    """Lost reviewer coverage degrades a run; it must not be laundered into success."""
    scenario = SCENARIOS["reviewer_timeout_degrades"]
    loaded = client.post(
        f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:reviewer_timeout_degrades"}
    ).json()
    run = start_run(
        client,
        project_id=project["id"],
        source_id=loaded["id"],
        scenario="reviewer_timeout_degrades",
        rounds=scenario.max_revision_rounds,
    )
    detail = wait_for_terminal(client, run["id"], timeout=120)
    assert detail["run"]["status"] == "degraded"

    inspection = client.get(f"/api/v1/workflows/{run['id']}/inspection").json()
    coverage = inspection["coverage"]
    assert coverage["failed"], "the degraded run must record which reviewer was lost"
    assert inspection["failed_reviewers"], "the failure reason must be preserved"
    assert inspection["run"]["status"] != "succeeded"
