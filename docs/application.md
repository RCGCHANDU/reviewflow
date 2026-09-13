# ReviewFlow web application

A local-first application around the existing `reviewflow` engine: create a review project,
point it at source material, pick reviewers and models, start a run, and watch four
independent reviewers, a synthesis, a controlled edit, an adversarial reviewer and a
verifier work through the document — with every stage's evidence inspectable and the
terminal state stated honestly.

The engine is unchanged in behaviour. This application is a layer over it.

- [1. Architecture](#1-architecture)
- [2. The domain/application boundary](#2-the-domainapplication-boundary)
- [3. Running it](#3-running-it)
- [4. Screens](#4-screens)
- [5. API](#5-api)
- [6. Workflow execution and live events](#6-workflow-execution-and-live-events)
- [7. Providers and model routing](#7-providers-and-model-routing)
- [8. Security model](#8-security-model)
- [9. Persistence](#9-persistence)
- [10. Tests](#10-tests)
- [11. Hosted deployment](#11-hosted-deployment)
- [12. Known limitations](#12-known-limitations)

## 1. Architecture

```
Frontend (React + TypeScript, Vite)
    |  fetch/JSON + Server-Sent Events
Application API (FastAPI, /api/v1)
    |
Workflow service (projects, sources, runs, providers)
    |
ReviewFlow engine (deterministic state machine + Agents SDK)
    |
ModelRouter -> providers (codex OAuth | commandcode | OpenAI API key)

Workflow service -> persistence (SQLite: projects, sources, runs, events, artifacts)
Workflow service -> the engine's own audit ledger (SQLite) + artifact files
```

Two rules hold the layering together:

1. **The frontend observes and commands; it never implements workflow rules.** No stage
   ordering, no verification logic, no "is this a success" judgement lives in TypeScript.
   The browser renders what the engine recorded.
2. **Nothing in the application recomputes what the engine already decided.** Statuses,
   verification results, usage and coverage are read from the engine's own evidence
   (`stage_outputs.json`, the audit ledger), not re-derived.

## 2. The domain/application boundary

The application adds one package, `reviewflow_app`, which imports the engine and never
copies it:

| Layer | Module | Responsibility |
| --- | --- | --- |
| API | `api/v1.py` | HTTP: validation, status codes, SSE framing |
| Services | `services/runs.py`, `services/providers.py` | Build engine settings, own run lifecycle, explain outcomes |
| Execution | `execution/executor.py` | Run workflows off the request thread; cancellation; durable terminal state |
| Events | `events.py` | Engine facts -> persisted events -> live streams |
| Persistence | `persistence/` | SQLite schema and the only SQL in the codebase |
| Security | `security/paths.py` | Filename sanitisation, workspace containment, atomic writes |
| Tracing | `tracing.py` | Route span telemetry to the right run's ledger, locally |

### Engine changes required (all additive, all default-off)

The engine needed three small extensions. Each defaults to the previous behaviour, so
every pre-existing test and eval is unaffected.

1. `WorkflowContext.stage_outputs` + `ReviewWorkflow._capture(...)` — the engine already
   computed reviewer reports, the issue map, the edit result, the diff evidence, the
   adversarial verdict, verification, coverage and the final report; it now records them so
   a finished run stays inspectable. They are persisted as `stage_outputs.json`.
2. `ReviewWorkflow(event_sink=...)` — an optional observer. `SdkAgentInvoker` now also
   reports `agent_started`, which is what lets the UI show *which* reviewers are running
   instead of inferring concurrency from completion order.
3. `ReviewWorkflow._notify(...)` milestones (`review.report_received`,
   `synthesis.completed`, `edit.completed`, `diff_review.completed`,
   `adversarial.completed`, `verification.completed`) so the live view updates as evidence
   arrives rather than only at stage boundaries.

The engine's orchestration, guardrails, permissions and verification logic are untouched.

## 3. Running it

Prerequisites: Python 3.11+, Node 20+.

```bash
# 1. Backend (engine + application extra)
uv venv .venv
uv pip install --python .venv -e ".[app,dev]"

# 2. Frontend
cd frontend
npm install
npm run build          # or: npm run dev  (proxies /api to the backend)
cd ..

# 3. Serve API + built frontend from one process
./.venv/Scripts/reviewflow-app --port 8000        # Windows
# ./.venv/bin/reviewflow-app --port 8000          # POSIX
```

Open <http://127.0.0.1:8000>. Data (projects, runs, artifacts) lands in
`artifacts/app/` unless `--data-root` / `REVIEWFLOW_APP_DATA_ROOT` says otherwise.

Development, two processes:

```bash
./.venv/Scripts/reviewflow-app --port 8000         # terminal 1
cd frontend && npm run dev                          # terminal 2 -> http://127.0.0.1:5173
```

### Provider credentials

Nothing is pasted into the application. Credentials are discovered from the stores the
existing CLI tools already maintain, exactly as the engine's CLI does:

| Provider | Source | Transport |
| --- | --- | --- |
| `codex` | `REVIEWFLOW_CODEX_AUTH_PATH`, else the Hermes store, else `~/.codex/auth.json` | Responses API, **streaming required**, `store=false` |
| `commandcode` | `REVIEWFLOW_COMMANDCODE_AUTH_PATH`, else `~/.commandcode/auth.json` | Chat Completions |
| `openai` | `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`) | Responses or Chat Completions |

If no provider is configured, `Settings -> Providers` says so per provider, and a live run
is refused with an explanation instead of failing later. **Scripted** runs need no
credential at all.

## 4. Screens

| Route | Purpose |
| --- | --- |
| `/` | Dashboard: recent runs with status, provider health, New review |
| `/new` | New review: source, objective, reviewers, provider/model routing, limits, preflight |
| `/runs` | Run history: filter by status, project, free text |
| `/runs/:id` | The run: timeline, reviewer findings, synthesis, edit + diff, adversarial, verification, result, raw events |
| `/projects`, `/projects/:id` | Projects, their sources and their runs |
| `/providers` | Provider status, capability checks (doctor), model catalogue, role → model defaults |

The run screen's tabs mirror the engine's evidence, one for one. Reviewers are shown
**separately**, per reviewer, until the synthesis tab — the UI never merges independent
reports, and never implies consensus the synthesis did not record.

## 5. API

All endpoints are versioned under `/api/v1`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | App + database + active run count |
| GET | `/providers` | Safe provider status (masked), never credentials |
| POST | `/providers/refresh` | Re-run discovery |
| POST | `/providers/doctor` | Live capability probe (real, billable calls) |
| GET | `/models` | `?provider=`, `?catalogue=true` for a live catalogue fetch |
| GET | `/roles` | Reviewer roles, tiers, defaults, mandatory status |
| GET/POST | `/projects` | List / create |
| GET | `/projects/{id}` | Project with sources and recent runs |
| POST | `/projects/{id}/sources` | Upload (multipart), sanitised and hashed |
| GET | `/projects/{id}/sources` | List sources |
| GET | `/samples`, POST `/projects/{id}/samples` | Deterministic sample material |
| POST | `/workflows/preflight` | Validate source/objective/providers before spending |
| POST | `/workflows` | Create and start a run |
| GET | `/workflows` | History (`?project_id=`, `?status=`, `?limit=`) |
| GET | `/workflows/{id}` | Run + stage timeline + `is_running` |
| GET | `/workflows/{id}/inspection` | Full evidence bundle |
| GET | `/workflows/{id}/events` | **SSE** stream, replayable via `Last-Event-ID` |
| GET | `/workflows/{id}/issues` | Findings flattened, with reviewer and disposition |
| POST | `/workflows/{id}/cancel` | Real cancellation |
| POST | `/workflows/{id}/rerun` | New run, changed configuration |
| GET | `/workflows/{id}/artifacts` | Artifact index |
| GET | `/workflows/{id}/artifacts/{aid}/download` | Download (containment-checked) |
| GET | `/workflows/{id}/download/diff` | The unified diff as a patch |

Errors are uniform and safe:

```json
{"error": {"code": "provider_unavailable", "message": "No provider credential is available…",
           "retryable": false, "detail": {"provider": "codex"}}}
```

## 6. Workflow execution and live events

A run is never executed inside an HTTP request. `POST /workflows` persists the run, returns
`201` immediately, and hands it to a `WorkflowExecutor`:

```python
class WorkflowExecutor(Protocol):
    async def submit(self, run_id: str) -> None: ...
    async def cancel(self, run_id: str) -> bool: ...
    def is_running(self, run_id: str) -> bool: ...
```

`InProcessExecutor` is the first implementation: one asyncio task per run, bounded
concurrency, cancellation that settles, a reaper so a task's exception can never vanish,
and an `on_unhandled` hook that records a truthful failure for any escape — a run left
non-terminal by a crash is reported as *not executing*, never as running. Swapping in a
worker process or task queue means implementing three methods; nothing else changes.

Events flow:

```
engine ledger .record()  ─┐
engine event_sink        ─┴─> RunRecorder ─> run_events table (durable, ordered)
                                          └─> EventBus ─> SSE subscribers
```

Every event has an id, run id, monotonic `seq`, timestamp, type, stage and a safe payload.
Sequence numbers are contiguous from 1, which is what makes replay-after-reconnect exact
and testable. On connect, the stream replays persisted events from `Last-Event-ID` and then
follows live events, ending with `stream.end`.

The vocabulary is closed and asserted by test (`reviewflow_app.events.EVENT_TYPES`):

```
workflow.created|started|succeeded|blocked|degraded|failed|cancelled|terminal
stage.started|completed|transition
agent.started|completed|failed|retried|invalid_output
review.report_received  synthesis.completed  edit.completed  edit.mutation
diff_review.completed   adversarial.completed
verification.check      verification.completed
guardrail.triggered     tool.called          stream.end
```

Only facts and structured outputs are streamed. No chain-of-thought, no prompts, no
provider payloads.

## 7. Providers and model routing

The UI never hard-codes a model name. Everything comes from

- `GET /providers` — which providers exist, whether a credential was found, auth source,
  masked account, expiry status, transport (`api_mode`, whether streaming is required),
  and which capability checks have passed for this process;
- `GET /models` — the provider's own catalogue when `?catalogue=true` (fetched live), plus
  the role defaults the router would use;
- `GET /roles` — role → tier → default model, straight from `AgentFactory.model_for`, so
  the diagnostic table cannot drift from what the engine actually builds.

Routing options per role: `auto` (the router's choice for the selected provider) or an
explicit `provider:model`. `provider` may be `auto`, `codex`, `commandcode` or `openai`.
Transport follows the provider, not the call site: a provider that requires streaming gets
streaming for every agent invocation, including guardrails and the agent-as-tool
adjudicator (this is enforced by one shared `execute_run(..., requires_streaming=...)`).

Capability honesty: a model the account cannot use is reported as such. `commandcode`
`gpt-5.4-mini` returned `403 MODEL_NOT_IN_PLAN` on the machine this was developed on, so it
is not a default anywhere; the doctor reports the failure rather than a generic error.

`POST /providers/doctor` makes **real, billable calls**. It is never run implicitly.

## 8. Security model

**Credentials are server-side only.**

- No endpoint returns a token, key, refresh token or JWT. Provider status carries the auth
  *source*, a masked account identifier (≤9 visible characters) and an expiry *status*.
- Secrets are never written to prompts, artifacts, the audit ledger or logs.
- Credential stores are read-mostly; if a refresh is persisted it is written byte-exactly
  with a `.bak` alongside, and every store path is overridable so tests never touch a real
  store.
- Tests assert this structurally: every response body is scanned for JWT/key shapes.

**Uploads and paths.**

- Filenames are sanitised (basename only, conservative charset, length-bounded); a client
  filename is never used to build a path.
- Internal ids are generated server-side; the stored path is derived from the id.
- Containment is enforced with `assert_within(path, root)` on every read and write, which
  rejects `..`, absolute escapes and symlink-free path tricks.
- Uploads are size-limited and suffix-checked; content is hashed (sha256) and the original
  snapshot is immutable — runs edit a copy in the run workspace.
- Writes by the engine are atomic (temp file + replace), so a cancellation can never leave a
  half-written document.

**Write isolation.** Only the Controlled Editor holds mutation capability, enforced by the
engine's registry/permission layer, not by the frontend. The API cannot widen it: run
parameters cannot grant tools, change allowed write roots, add reviewers with write access,
or bypass guardrails.

**Prompt injection.** Uploaded content is untrusted data in the engine's prompts. The
application exposes no route that lets document content influence credentials, system
prompts, tool permissions, allowed paths, workflow states or verification criteria.

## 9. Persistence

SQLite (`<data_root>/app.sqlite3`, WAL), five tables: `projects`, `sources`, `runs`,
`run_events`, `artifacts`. The engine's audit ledger stays authoritative for event-level
detail and lives with the run's artifacts; the application database holds the objects the
application needs to list, resume and explain runs.

Nothing important lives in the browser. Close the tab, restart the process, open the run
later: status, stage timeline, evidence, usage and artifacts are all read back. A test
covers exactly that (a second app instance over the same data root sees the first run's
terminal state and replays its event stream).

Per run, on disk:

```
artifacts/app/runs/<run_id>/workspace/manuscript.md        <- edited working copy
artifacts/app/runs/<run_id>/workspace/artifacts/audit.sqlite3
artifacts/app/runs/<run_id>/workspace/artifacts/<wf_id>/audit_events.json
artifacts/app/runs/<run_id>/workspace/artifacts/<wf_id>/stage_outputs.json
artifacts/app/sources/<source_id>/manuscript.md            <- immutable ingested snapshot
```

Artifacts are indexed with type, filename, size and sha256, and downloaded through a
containment-checked endpoint.

## 10. Tests

```bash
# Backend: engine + application + integration
./.venv/Scripts/python.exe -m pytest tests -q

# Frontend
cd frontend && npm run typecheck && npm run lint && npm test && npm run build
```

Coverage highlights:

- **Application** (`tests/app/`): project creation, upload validation, filename traversal,
  oversize and suffix rejection, run creation, SSE replay, cancellation, provider
  serialization, "no secrets in any response", role-model override validation, persistence,
  artifact retrieval, event-vocabulary closure, and that an artifact-bookkeeping failure
  cannot rewrite a verified verdict.
- **Status preservation** (`tests/app/test_status_preservation.py`): eval scenarios are run
  *through the API* and the application's terminal status must equal the engine's expected
  status — blocked stays blocked, degraded stays degraded, a verification failure can never
  become success.
- **Integration** (`tests/integration/test_full_stack_e2e.py`): the full chain — project,
  upload, preflight, start, live events, four reviewers, synthesis, edit, diff, adversarial,
  verification, terminal state, artifact download, reopen later — plus a durability test
  across process restarts, and a test that the built frontend is actually served.
- **Frontend**: new-review validation, provider-unavailable handling, live timeline updates
  from streamed events, reviewer separation, diff rendering, failed verification display,
  blocked terminal state, reconnecting to an existing run, run history.

No test needs a provider credential or a paid call: runs use the engine's scripted model.
Live-provider tests are separate and explicit (`-m integration` for the engine's own suite).

## 11. Hosted deployment

The seams already in place for a hosted version, and what they would replace:

| Today | Hosted |
| --- | --- |
| `InProcessExecutor` | worker process / queue implementing `WorkflowExecutor` |
| SQLite in `data_root` | Postgres behind the same repositories |
| local source store | object storage; `SourceArtifact.stored_path` becomes a storage key |
| operator-supplied credentials | server-managed encrypted credentials per account |
| single user | projects and runs already carry ids; add ownership and auth middleware |
| `EventBus` in-process | Redis/pub-sub fan-out behind the same interface |

The domain model deliberately stores ids and relative storage references rather than
browser-supplied absolute paths, so none of this requires reshaping the entities.

## 12. Known limitations

- **Scripted runs replay a canned scenario.** They exercise the real engine, event pipeline,
  ledger and artifacts with a deterministic model, but the canned edits are authored against
  a file named `manuscript.md` and a specific sample text. Scripted mode is for trying and
  testing the application, not for reviewing your own document — the UI says so. Use a live
  provider for real material.
- **Reviewers cannot be disabled.** The engine treats its four reviewers as mandatory (the
  coverage gate would degrade a run that skipped one). The API validates a requested
  reviewer set and rejects one that omits a mandatory reviewer, naming it. The UI therefore
  shows them as required rather than offering a control that would not be honoured.
- **No CANCELLED state in the engine.** Cancellation is an application terminal state
  (`cancelled`) recorded by the executor, never reported as `failed`. The engine's own
  `WorkflowStatus` is unchanged, which is why the mapping is documented rather than faked.
- **Cancellation is cooperative.** In-flight model calls are cancelled at await points;
  an atomic file replace is never interrupted.
- **No cost in currency.** Usage shows calls, tokens, latency and per-agent breakdown. Cost
  is reported as `unknown` because no pricing table is available for OAuth-backed gateways;
  the schema has a place for it.
- **`doctor` and live runs are billable** and slower on providers that require streaming.
- **The OAuth refresh path is tested against a stubbed token endpoint**, not the live auth
  server; a stale credential surfaces as a provider error, not a silent retry loop.
- **Single-process.** The application is a local-first deployment; concurrency limits are
  per process (`max_concurrent` runs), and horizontal scaling needs the hosted seams above.
