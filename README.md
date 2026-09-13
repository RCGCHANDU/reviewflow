# reviewflow

A production-oriented multi-agent review framework built on the **OpenAI Agents SDK for Python**.

It runs a rigorous pipeline over a knowledge-work artifact:

```
INGEST -> PLAN -> PARALLEL_REVIEW -> SYNTHESIZE -> CONTROLLED_EDIT -> DIFF_REVIEW
       -> ADVERSARIAL_REVIEW -> VERIFY -> PASS (FINAL_REVIEW -> COMPLETE)
                                      \-> REVISE -> SYNTHESIZE (bounded loop)
                                      \-> BLOCKED
```

Four independent reviewers inspect the same material, synthesis reconciles them, **one** agent is
allowed to edit, and the revision must survive diff review, adversarial falsification and
verification before the workflow may report success.

The design goal is that the safety properties are enforced by **code**, not by prompts:

| Guarantee | How it is enforced |
|---|---|
| Reviewers cannot write | built without mutating tools; an SDK tool guardrail refuses mutating calls; the write tool demands an edit grant |
| Reviewers are independent | each gets its own packet built from the plan and material only; no peer conclusions ever enter it |
| One writer at a time | the edit stage holds an `asyncio.Lock`; grants cover exactly one path at one hash |
| Nothing outside the grant changes | workspace snapshots before/after the edit; a diff that touches anything else is a blocking violation |
| No fabricated edits | the diff is computed from real bytes; a claimed change whose text is absent from the result is rejected |
| No success without verification | completion gates require passed mandatory checks, an accepted adversarial verdict, zero blocking issues and full reviewer coverage |
| No swallowed failures | timeouts, malformed output, guardrail trips and missing reviewers become `FAILED`/`BLOCKED`/`DEGRADED`, never `SUCCEEDED` |
| Orchestration state is inspectable | explicit state machine + SQLite audit ledger; the chat transcript is never the database |

---

## Web application

`reviewflow` also ships as an application: a FastAPI service plus a React/TypeScript
interface over the same engine, where a review is a first-class object you can watch,
inspect and reopen.

```bash
uv pip install --python .venv -e ".[app,dev]"     # engine + application extra
cd frontend && npm install && npm run build && cd ..
./.venv/Scripts/reviewflow-app --port 8000        # serves the API and the built UI
```

Open <http://127.0.0.1:8000> and create a review: pick a project, supply source material,
write the objective, choose reviewers and models, run preflight, start. The run screen shows
the stage timeline, each reviewer's independent findings, the synthesis (accepted, rejected,
merged, conflicting), the controlled edit with a real unified diff, the adversarial verdict,
every verification check, and the honest terminal state — `SUCCEEDED`, `BLOCKED`, `DEGRADED`
or `FAILED`, never a flattening "Completed". Live progress arrives over Server-Sent Events
and survives a page refresh or a restart.

See **[docs/application.md](docs/application.md)** for the architecture, API, security
model, persistence layout, test strategy and the list of known limitations.

The engine is unchanged in behaviour: the application imports it rather than forking it, and
the three small engine extensions it needed (`stage_outputs` capture, an optional event sink,
stage milestones) are additive and default-off.

## Install

```bash
uv venv .venv --python 3.11
uv pip install --python .venv -e ".[dev]"     # or: pip install -e ".[dev]"
```

## Providers: plug and play, no keys to paste

reviewflow talks to whichever model backend this machine is already signed in to. It
reads the credential stores the vendor CLIs wrote, so a machine that can run
`codex` or the commandcode CLI can run reviewflow with **no configuration at all**:

```bash
reviewflow providers     # what was discovered, and from where (secrets masked)
reviewflow doctor        # live end-to-end check: plain call, structured output, tool call
```

| Provider | Auth | Transport | What it serves |
|---|---|---|---|
| `commandcode` | `~/.commandcode/auth.json` (written by the CLI's login) | Chat Completions | the whole catalogue: `gpt-5.6-sol`, `gpt-5.6-luna`, `deepseek/deepseek-v4-flash`, `claude-sonnet-5`, `Qwen/*`, … |
| `codex` | ChatGPT OAuth (`~/.codex/auth.json`, or Hermes' store) | Responses | the models the ChatGPT account exposes: `gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-5.5`, … |
| `openai` | `OPENAI_API_KEY` | Responses | the plain API-key path |

Selection is one variable:

```bash
export REVIEWFLOW_PROVIDER=auto        # default: first credentialed provider in preference order
export REVIEWFLOW_PROVIDER=codex       # force the OpenAI ChatGPT OAuth path
export REVIEWFLOW_PROVIDER=commandcode # force the commandcode gateway
```

OAuth tokens are refreshed automatically when they are within two minutes of expiry, and
the refreshed token is written back (atomically, keeping one `.bak`) so the vendor CLI keeps
working too. Refresh failure degrades to "no credential" and falls back, rather than
crashing a run.

Any role can be pinned to a different backend with a `provider:model` prefix:

```bash
export REVIEWFLOW_REVIEWER_MODEL=codex:gpt-5.6-luna
export REVIEWFLOW_EDITOR_MODEL=commandcode:gpt-5.6-sol
```

`REVIEWFLOW_PROVIDER=openai` with `OPENAI_API_KEY` set remains fully supported; nothing
below requires an API key. Other settings live in `.env.example` (`REVIEWFLOW_*`).

## Run it

```bash
# 1. Scripted end-to-end demo: real workflow, canned model responses, no credential, no cost
python examples/run_end_to_end.py
reviewflow demo

# 2. Real review over OAuth (or an API key) - discovers its provider automatically
reviewflow run --target manuscript.md --objective "Find unsupported claims and fix the smallest defensible set."

# 3. Evaluations (scripted, deterministic, free)
reviewflow evals
reviewflow evals --only editor_fabricates_adversarial_catches

# 4. Inspect the audit ledger of any run
reviewflow audit --workflow-id wf_2dddd5a8a116
reviewflow roles      # role -> model -> tool policy table
reviewflow checks     # registered deterministic and semantic checks
```

Example output of the scripted demo:

```
status:   succeeded
reason:   all gates passed with full coverage
stages:   ingest -> plan -> parallel_review -> synthesize -> controlled_edit -> diff_review
          -> adversarial_review -> verify -> final_review -> complete
rounds:   1
coverage: expected=['evidence_auditor', 'structure_reviewer', 'technical_reviewer'] failed=[]
usage:    10 model calls, 1980 tokens, 170 ms
checks passed: target_exists, hash_matches_disk, workspace_unchanged_outside_grant, diff_scope,
   no_change_justified, forbidden_patterns, structural_preservation, python_syntax,
   markdown_internal_links, edit_addresses_accepted_issues, no_new_unsupported_claims,
   uncertainty_preserved
```

## Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │  ReviewWorkflow  (deterministic engine)      │
                    │  explicit state machine + audit ledger       │
                    └───┬──────────────────────────────────────────┘
   INGEST ── snapshot + material ─────────────────────────────────────────────┐
   PLAN ──── Agent[planner]  (input guardrail: task scope)                    │
   PARALLEL_REVIEW ── asyncio.gather (bounded by semaphore, per-agent timeout) │
        ├── Agent[evidence_auditor]      -> ReviewerReport   read-only         │
        ├── Agent[technical_reviewer]    -> ReviewerReport   read-only         │
        ├── Agent[rhetorical_analyst]    -> ReviewerReport   read-only         │
        └── Agent[structure_reviewer]    -> ReviewerReport   read-only         │
   SYNTHESIZE ── Agent[orchestrator] -> ConsolidatedIssueMap                   │
        └── as_tool: Agent[deep_dive_adjudicator]  (one conflict at a time)    │
   CONTROLLED_EDIT ── Agent[controlled_editor] (write lock + edit grant)       │
   DIFF_REVIEW ── Agent[diff_reviewer]        (code-computed diff)            │
   ADVERSARIAL_REVIEW ── Agent[adversarial_reviewer] (no peer verdicts)       │
   VERIFY ── deterministic checks + Agent[verifier] for semantic checks       │
   FINAL_REVIEW ── Agent[orchestrator] (output guardrail: status inflation)   │
                    └───────────────────────────────────────────────────────┘
```

**Orchestration choice: hybrid.** Deterministic Python drives every stage through direct
`Runner.run()`. Handoffs are used **nowhere**: they transfer conversational ownership, which would
let reviewer reasoning leak into the next agent and would hide the workflow's shape inside the model.
`Agent.as_tool()` is used **once**, for the deep-dive adjudicator, where *which* conflict needs
investigation is genuinely semantic and the orchestrator must keep ownership of the issue map.

**Transport is the provider's business.** Every model call - stage agents, the input guardrail, the
agent-as-tool adjudicator - goes through one helper (`orchestration/invoker.execute_run`) that picks
`Runner.run` or `Runner.run_streamed` from the routed provider's requirement. Backends differ: the
Codex OAuth backend rejects non-streaming requests outright, so "which transport" cannot be decided
by the caller without one of them breaking.

Details, including the verified SDK API surface and every version-dependent decision, are in
[`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md).

## Adding a specialist

Two edits, no engine change:

```python
# 1. src/reviewflow/prompts/roles.py - add a RolePromptSpec with the ten required blocks
SECURITY_REVIEWER = RolePromptSpec(role_key="security_reviewer", identity="...", ...)

# 2. src/reviewflow/agents/registry.py - register it
SpecialistSpec(
    role_key="security_reviewer",
    agent_name="Security reviewer",
    description="Looks for unsafe data handling and injection surfaces.",
    focus=(IssueCategory.OTHER,),
)
```

The planner can now select it, `reviewflow roles` shows its policy, and the read-only assertion
covers it automatically. If you register a specialist whose prompt spec is missing, registration
fails immediately.

## Adding a tool

```python
from agents.decorators import tool
from reviewflow.permissions import register_tool

@tool
async def git_log(ctx: RunContextWrapper[WorkflowContext], path: str, limit: int = 20) -> str:
    """Return recent commit subjects for a path."""
    ...

register_tool("git_log", mutating=False)   # fail closed: unregistered tools are refused
```

Then add the name to the role's policy (`SpecialistSpec.allowed_tools` / `RolePolicy.allowed_tools`).
Attaching it without registering its mutability makes any read-only agent holding it refuse to build.

## Changing a prompt

Prompts live in `src/reviewflow/prompts/`. `roles.py` holds one spec per role; `library.py` composes
the ten blocks and generates the **output contract from the Pydantic schema**, so a prompt cannot
describe a field the validator does not enforce. Run `reviewflow evals` afterwards: the criteria are
behavioural (status, ledger events, coverage, artifact contents), so a prompt change is accepted only
if the guarantees still hold.

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q          # 178 passed, 2 skipped, no API calls, no credentials
.venv/Scripts/python -m pytest tests/ -q -m integration   # excluded by default
```

The suite is hermetic: it drives the **real** SDK (`Runner.run`, real structured-output
validation, the real tool pipeline and guardrails) against a scripted model provider, so it needs
no network, no API key and no credential store. Provider tests redirect every credential store at
a temporary path, and one of them hashes the real store before and after a token refresh to prove
the redirect cannot be escaped.

Live check of the providers themselves (real calls, no key needed on an OAuth machine):

```bash
reviewflow doctor                       # plain + structured + tools, per provider and model
reviewflow doctor --provider codex      # just the OpenAI ChatGPT OAuth path
```

Live-API integration test (opt in twice, so a normal run can never spend credits):

```bash
OPENAI_API_KEY=sk-... REVIEWFLOW_LIVE_TEST=1 .venv/Scripts/python -m pytest tests/ -q -m integration
```

## Evaluations

`reviewflow evals` runs nine scenarios through the real engine with a scripted model. Each asserts
observable behaviour and ledger content:

| Scenario | Expected | What it proves |
|---|---|---|
| `clean_input_no_edit` | succeeded | a no-edit disposition is still adversarially reviewed and verified |
| `conflicting_reviewers` | blocked | an unresolved disagreement cannot be smoothed into success |
| `unsupported_claim_detected_and_fixed` | succeeded | detection → acceptance → edit → verification |
| `reviewer_hallucination_rejected` | succeeded | a fabricated issue is rejected during synthesis and disclosed |
| `editor_fabricates_adversarial_catches` | succeeded | round 1 fabricates, adversarial + semantic checks reject, round 2 corrects |
| `failed_verification_blocks_success` | blocked | a failed mandatory check can never become success |
| `reviewer_timeout_degrades` | degraded | lost coverage is reported as degraded, never as success |
| `malformed_output_recovered` | succeeded | bounded corrective retry, recorded with `retry_count` |
| `successful_end_to_end` | succeeded | all nine stages, two edits, full ledger |

## Layout

```
src/reviewflow/
  config.py            environment-driven settings (models, budgets, safety)
  context.py           typed runtime context, edit grants, hashing
  errors.py            error hierarchy
  permissions.py       role policies; fail-closed tool mutability registry
  models/              enums + structured contracts (Pydantic output types)
  agents/              specialist registry + agent factory (build-time policy assertions)
  prompts/             role prompt specs + schema-derived output contracts
  tools/               read-only fs tools + the single mutating tool
  guardrails/          task-scope input guardrail, status output guardrail, tool guardrails
  validation/          application validators between a model output and a transition
  orchestration/       state machine, ingest, packets, invoker, limits, engine
  verification/        deterministic checks, semantic check plumbing, diffs, snapshots
  providers/           plug-and-play model backends: OAuth + API-key discovery, routing, doctor
  persistence/         ledger interface + SQLite / JSONL / in-memory backends
  observability/       logging + SDK trace -> ledger processor
  evals/               scenarios + harness
  simulate.py          scripted model (routing by agent marker) for tests and evals
  testkit.py           workspaces, scripted runs, tool-call helper
  fixtures.py          declarative payload builders
  cli.py               run / demo / evals / providers / doctor / audit / checks / roles
tests/                 178 passed, 2 skipped (hermetic: no network, no credentials)
examples/              runnable end-to-end example + sample manuscript
tools/                 provider probes / credential inspector (development diagnostics)
```
