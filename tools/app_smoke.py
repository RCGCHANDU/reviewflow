"""API-level end-to-end smoke: project -> sample -> scripted run -> evidence -> artifacts.

Uses the deterministic scripted engine, so it is free and repeatable. Run:
    python tools/app_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from reviewflow_app.config import AppSettings
from reviewflow_app.main import create_app


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/app-smoke").resolve()
    app = create_app(AppSettings(data_root=root, static_dir=None))
    with TestClient(app) as client:
        project = client.post("/api/v1/projects", json={"name": "Smoke", "description": "api smoke"}).json()
        print("project:", project["id"])

        source = client.post(
            f"/api/v1/projects/{project['id']}/samples", params={"sample_id": "scenario:successful_end_to_end"}
        ).json()
        print("source:", source["filename"], source["sha256"][:12], f"{source['size_bytes']} bytes")

        preflight = client.post(
            "/api/v1/workflows/preflight",
            json={
                "project_id": project["id"],
                "source_id": source["id"],
                "objective": "Detect unsupported claims and make only defensible edits.",
                "config": {"mode": "scripted"},
            },
        ).json()
        print("preflight ok:", preflight["ok"], "| blocking:", preflight["blocking_failures"])

        run = client.post(
            "/api/v1/workflows",
            json={
                "project_id": project["id"],
                "source_id": source["id"],
                "objective": "Detect unsupported claims and make only defensible edits.",
                "config": {"mode": "scripted", "scripted_scenario": "successful_end_to_end"},
            },
        ).json()
        run_id = run["id"]
        print("run:", run_id, run["status"], flush=True)

        deadline = time.time() + 60
        status = run["status"]
        while time.time() < deadline:
            detail = client.get(f"/api/v1/workflows/{run_id}").json()
            status = detail["run"]["status"]
            if status in {"succeeded", "blocked", "degraded", "failed", "cancelled"}:
                break
            time.sleep(0.2)
        print("terminal status:", status, flush=True)

        print("reading event stream...", flush=True)
        events = client.get(f"/api/v1/workflows/{run_id}/events", timeout=30.0).text
        types = [line.split("event: ")[1].strip() for line in events.splitlines() if line.startswith("event: ")]
        print(f"events: {len(types)} | first: {types[:4]} | last: {types[-3:]}", flush=True)

        inspection = client.get(f"/api/v1/workflows/{run_id}/inspection").json()
        print("stages:", [(s["stage"], s["state"]) for s in inspection["stages"] if s["state"] != "pending"])
        print("reviewers:", [r["reviewer"] for r in inspection["reviewers"]])
        print("issues:", sum(len(r["issues"]) for r in inspection["reviewers"]))
        print("synthesis accepted:", len((inspection["synthesis"] or {}).get("accepted_issues") or []))
        print("diff chars:", len(inspection["diff"] or ""))
        print("adversarial passed:", (inspection["adversarial"] or {}).get("passed"))
        print("verification:", len((inspection["verification"] or {}).get("results") or []), "checks")
        print("explanation:", inspection["status_explanation"][:200])
        print("artifacts:", [(a["type"], a["filename"]) for a in inspection["artifacts"]])

        if inspection["artifacts"]:
            first = inspection["artifacts"][0]
            download = client.get(f"/api/v1/workflows/{run_id}/artifacts/{first['id']}/download")
            print("download:", download.status_code, f"{len(download.content)} bytes")

        issues = client.get(f"/api/v1/workflows/{run_id}/issues").json()
        print("issues endpoint: total", issues["total_issues"], "| dispositions", len(issues["dispositions"]))

        report = {
            "run_id": run_id,
            "status": status,
            "event_types": types,
            "inspection_keys": sorted(inspection),
        }
        out = root / "smoke_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("report:", out)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
