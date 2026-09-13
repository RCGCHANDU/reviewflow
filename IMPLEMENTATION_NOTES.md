# Implementation notes

Version-sensitive facts, deliberate deviations from the obvious approach, and the reasoning behind
decisions that a future maintainer would otherwise have to re-derive.

---

## 1. SDK verification (what was checked, and how)

Everything below was verified against the **installed** package, not from memory:

```bash
python -c "import agents, importlib.metadata as md; print(md.version('openai-agents'))"
# 0.22.2
```

| API | Verified shape (0.22.2) |
|---|---|
| `Agent` | dataclass with `name, handoff_description, tools, mcp_servers, instructions, prompt, handoffs, model, model_settings, input_guardrails, output_guardrails, output_type, hooks, tool_use_behavior, reset_tool_choice` |
| `Runner` | `run`, `run_sync`, `run_streamed`. `run(starting_agent, input, *, context, max_turns=10, hooks, run_config, error_handlers, previous_response_id, auto_previous_response_id, conversation_id, session)` |
| `Agent.as_tool` | `(tool_name, tool_description, custom_output_extractor=None, is_enabled=True, on_stream=None, run_config=None, max_turns=None, hooks=None, previous_response_id=None, conversation_id=None, session=None, failure_error_function=..., needs_approval=False, parameters=None, input_builder=None, include_input_schema=False) -> FunctionTool`. Nested run; **parent conversation state is not inherited**. |
| `RunConfig` | includes `model, model_provider, model_settings, input_guardrails, output_guardrails, tracing_disabled, tracing, trace_include_sensitive_data, workflow_name, trace_id, group_id, trace_metadata, tool_execution, tool_error_formatter, sandbox, session_input_callback` |
| `Model` / `ModelProvider` | `Model.get_response(system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, *, previous_response_id, conversation_id, prompt) -> ModelResponse`; `ModelProvider.get_model(model_name)` |
| `ModelResponse` | a **pydantic** dataclass: `output: list[TResponseOutputItem]`, `usage`, `response_id` (required), `request_id`, `raw_usage`. Output items must be real Responses-API objects (`ResponseOutputMessage`, `ResponseFunctionToolCall`), not plain dicts. |
| tools | `from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail`; a first parameter annotated `RunContextWrapper[...]` or `ToolContext` is injected. `ToolContext` exposes `context`, `tool_name`, `tool_call_id`, `tool_arguments`, `tool_call`, `agent`, `run_config`. |
| guardrails | input guardrails run only for the **first** agent of a chain; output guardrails only for the agent producing the **final** output; tool guardrails run on **every** guarded tool call. |
| tracing | `set_trace_processors([...])`, `TracingProcessor` ABC (`on_trace_start/end`, `on_span_start/end`, `shutdown`, `force_flush`); `Span` exposes `trace_id`, `span_id`, `parent_id`, `started_at`, `ended_at`, `span_data.type`, `error`, `trace_metadata`. |
| sessions | `SQLiteSession(session_id, db_path=":memory:", ...)`; a session cannot be combined with `conversation_id` / `previous_response_id` in the same run. |

### Spikes that drove the design

Each of these was executed before the corresponding design decision:

1. **Structured output through a stub model** - `RunConfig(model_provider=...)` with a custom `Model`
   returning JSON text produces a validated Pydantic `result.final_output`. Malformed text raises
   `ModelBehaviorError("Invalid JSON when parsing model output")`. → This is why the whole test suite
   can exercise real SDK paths with no network, and why malformed output is an `INVALID_OUTPUT`
   failure class rather than a crash.
2. **Tool loop + hooks** - a scripted function call executes, its result is fed back, and `RunHooks`
   fire in the order `agent_start → llm_start/end → tool_start/end → llm_start/end → agent_end`.
3. **Tool input guardrail** - `ToolGuardrailFunctionOutput.reject_content(...)` reaches the model as a
   refusal *before* the tool body runs. → used as the second gate on the write tool.
4. **`Agent.as_tool()`** - a nested run with its own instructions and its own final output, while the
   parent keeps the conversation. → used for the deep-dive adjudicator.
5. **Concurrency** - `asyncio.gather` over four `Runner.run` calls works. → used for parallel review.

### Model names

Taken from the official Agents SDK *Models* page for this version, never invented:

* SDK default model: `gpt-5.4-mini` (`reasoning.effort="none"`, `verbosity="low"`)
* documented higher-quality option: `gpt-5.6-sol`

Defaults in `config.py`: reviewers and mechanical checks use `gpt-5.4-mini`; orchestrator, editor and
adversarial reviewer use `gpt-5.6-sol`. Every role is overridable (`REVIEWFLOW_*_MODEL`). No
`ModelSettings` are set by default: the SDK already applies tuned GPT-5 defaults, and overriding
reasoning/verbosity without measurement would be guesswork. `REVIEWFLOW_MODEL_SETTINGS_JSON` exists
for measured tuning.

---

## 2. Deliberate deviations from the obvious implementation

**No handoffs, anywhere.** Handoffs move conversational ownership to the specialist. In this workflow
that would (a) let one reviewer's reasoning reach the next agent, destroying the independence property
the design exists to provide, and (b) put the workflow's shape inside the model instead of the state
machine. Handoffs are supported by the SDK; they are simply the wrong primitive here.

**`Agent.as_tool()` exactly once.** The synthesizer can consult a deep-dive adjudicator. The choice of
*which* unresolved conflict deserves investigation is semantic (an LLM call), while the orchestrator
must keep ownership of the final issue map (so a tool, not a handoff).

**Sessions: off by default.** One shared session across specialists would be the single easiest way to
destroy reviewer independence, so it is never done. Even for the orchestrator, conversational state is
rejected in favour of explicit, ledger-recorded stage packets: the ledger is the database, and a
session would hold history in a form the audit trail cannot reproduce exactly. The invoker supports a
session, and `run_scripted`/tests exercise the isolation requirement instead.

**SDK guardrails are used only where their semantics hold.** Because input guardrails run only on the
first agent and output guardrails only on the final-output agent, they are attached to the entry point
(planner) and the exit point (final report). Intermediate specialist output is validated by
`reviewflow.validation` + tool guardrails + state-transition checks. Attaching a guardrail to every
agent and assuming it validates every intermediate call is a documented misunderstanding, avoided here.

**Conservative repairs are allowed, silent coercion is not.** If a reviewer files a *blocking* issue
whose `evidence_status` cannot support it, the validator retries once with corrective feedback and, if
it still fails, **downgrades it to minor and records why** in `validation_notes` and the ledger. The
finding stays visible; it just stops being blocking. Dropping it would hide a signal, and "fixing" it
silently would be worse.

**A missing reviewer cannot be a success.** Reviewer coverage is tracked per role; a reviewer that
never ran is recorded as *failed* (`"did not run"`), and any failed reviewer or truncated material
yields `DEGRADED`, which is deliberately not `SUCCEEDED`. `WorkflowResult.ok` is true only for
`SUCCEEDED`, so a caller cannot accidentally treat degraded as clean.

**Blocked and failed runs still get a report.** If the orchestrator cannot produce one, the workflow
writes an honest fallback (status, reason, unresolved issues, disclosures). The `status` field of
`WorkflowResult` is always computed by code; the model's `claimed_status` is only an assertion, and an
output guardrail trips when it exceeds the computed one.

**The no-edit path is adversarially reviewed.** "Nothing needs changing" is a real failure mode, so the
adversarial reviewer is asked to attack *that decision*, and a deterministic check
(`no_change_justified`) fails if an accepted action-required issue exists while no edit was applied.

---

## 3. Windows-specific finding

The first end-to-end run failed verification with `hash_matches_disk`. Cause: `Path.read_text()` uses
universal newlines (`\r\n` → `\n`) while the default text-mode write translates `\n` → `os.linesep`, so
on Windows an edit silently rewrote every line ending and changed the file's hash.

Fix: the editor reads **bytes**, operates on the decoded text, encodes back, and writes through
`tempfile.mkstemp` + `os.fdopen(..., "wb")` + `os.replace`. The recorded diff hashes come from the
bytes on disk (`with_disk_hashes`), not from a re-encoded string. A regression test writes a CRLF file
and asserts the bytes survive exactly.

---

## 4. Cost and token control

* `max_turns_per_agent` (default 8) on every call → a tool-looping model cannot run away.
* `max_revision_rounds` (default 2) → the revision loop is bounded; the gate uses
  `round_index < max_rounds + 1`, so rounds never silently exceed the budget.
* Bounded retries split by failure class (`RetryLimits`): transient 2, invalid output 1, timeout 1,
  logical 0, guardrail 0, infrastructure 0. Logical failures are never retried; verification failures
  trigger correction (a revision round), not a blind retry of the same call.
* `max_reviewer_concurrency` (default 4) with a semaphore.
* One verifier call answers every semantic check (`VerificationBatch`) instead of one call per check.
* Reviewers receive the bounded material (60k characters max) plus their unit assignment, not the whole
  repository; a search tool exists for targeted retrieval.
* `TokenBudget` + `WorkflowResult.usage` (per role, per call, tokens and latency) make spend visible.
* Models are per-role, so mechanical work can run on the cheap model.

---

## 5. Provider plug-and-play (OAuth, no keys pasted)

Everything in this section was established by calling the live endpoints, not by reading
documentation; the probe scripts are `tools/probe_providers.py`,
`tools/probe_sdk_codex.py`, `tools/probe_sdk_commandcode.py` and
`tools/probe_model_matrix.py`, and their JSON output is written to `artifacts/`.

### The seam

The Agents SDK resolves every agent's model *string* through `RunConfig.model_provider`,
so `ModelProvider` is the one interface that has to be implemented to swap backends.
`reviewflow.providers.routing.ModelRouter` implements it and returns an
`OpenAIResponsesModel` or an `OpenAIChatCompletionsModel` per provider. That is why no
agent, prompt or stage changed shape when providers were added: they still carry model
strings.

### Verified backend facts (each one cost a failed call to learn)

| Fact | Evidence | Consequence in code |
|---|---|---|
| The Codex backend rejects non-streaming requests | `400 {"detail": "Stream must be set to true"}` | `SdkAgentInvoker._run_once` uses `Runner.run_streamed` and drains `stream_events()` when the route requires it |
| The Codex backend rejects `store=true` (the SDK's default) | `400 {"detail": "Store must be set to false"}` | `ProviderProfile.store=False`, applied via `dataclasses.replace` in `model_settings_for` |
| The Codex backend needs an account header | call is rejected as unauthorised without `ChatGPT-Account-ID`, taken from the token's `https://api.openai.com/auth.chatgpt_account_id` claim | `codex_headers()` |
| A ChatGPT account does not expose every model | `gpt-5.4` → `"not supported when using Codex with a ChatGPT account"`; `GET /models` lists `gpt-6-astra, gpt-reserve, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, codex-auto-review` | Codex role defaults come from that list |
| commandcode is behind a WAF that blocks default user agents | `error code: 1010` for a plain urllib `User-Agent` | every client sets a real `User-Agent` |
| commandcode honours strict JSON-schema `response_format` | probe returned `{"verdict":"one","count":1}` | structured outputs work over Chat Completions |
| **Not every catalogue model is in the account's plan** | `gpt-5.4-mini` → `403 MODEL_NOT_IN_PLAN: GPT-5.4 Mini available in Pro and above plans` | `gpt-5.4-mini` is deliberately **not** a default anywhere on commandcode; `reviewflow doctor` exists to catch exactly this class of failure |
| `ModelSettings` is a dataclass, not a pydantic model | `hasattr(ModelSettings(), "model_copy") is False` | `dataclasses.replace`, not `.copy(update=...)` |
| `Agent` rejects an explicit `model_settings=None` | `TypeError: Agent model_settings must be a ModelSettings instance or a dict, got NoneType` | the kwarg is omitted rather than passed as `None` |

### Verified model matrix

Plain / structured / tools, per provider (`tools/probe_model_matrix.py`):

| Provider | Model | plain | structured | tools |
|---|---|---|---|---|
| commandcode | `gpt-5.6-sol` | ✓ | ✓ | ✓ |
| commandcode | `gpt-5.6-luna` | ✓ | ✓ | ✓ |
| commandcode | `deepseek/deepseek-v4-flash` | ✓ | ✓ | ✓ |
| commandcode | `gpt-5.4-mini` | ✗ 403 | ✗ | ✗ |
| codex | `gpt-5.6-sol` | ✓ | ✓ | ✓ |
| codex | `gpt-5.6-luna` | ✓ | ✓ | ✓ |

### Precedence

`model_overrides` (env, or an explicit choice) → a deliberately-set model that differs from
the provider-agnostic placeholder in `ModelsByRole` → the active provider profile's default.
The middle rule matters: `ModelsByRole`'s defaults are placeholders chosen without a provider
in mind, so letting them win would route every run to a model named for a different backend.
Override lookup is normalised to the role's *tier*, because `REVIEWFLOW_REVIEWER_MODEL` must
apply to `evidence_auditor` too.

### Credential handling rules

1. Nothing asks the user to paste a secret; credentials come from the environment or from the
   store the vendor CLI already wrote.
2. Secrets never reach a prompt, a log, or a CLI report — the status helpers return masked
   forms, and a test asserts the full key is absent from the serialised status.
3. Refresh is automatic inside a 120-second skew, writes back atomically with a `.bak`, and
   degrades to "no credential" on failure.
4. **Store paths are always resolved through their overridable accessor.** An earlier version
   built the Codex store path as `Path.home() / ".codex" / "auth.json"` in the write-back
   path, so a test that had redirected the store still overwrote the developer's real ChatGPT
   login. It was restored byte-identically from the backup the same function creates, and the
   regression test now hashes the real store before and after a refresh.

### Tracing

When no OpenAI-platform key is present (an OAuth-only setup) the SDK's default trace exporter
could only fail on every span. `reviewflow run` therefore installs the ledger processor via
`set_trace_processors`, which replaces the exporter list, so tracing stays local: full spans
in SQLite, nothing leaving the host.

---

## 6. Known limitations

1. **Single-target ingest.** The engine reviews one artifact. Multi-file material would need a unit
   type that spans files; the diff/scope machinery already handles paths, but `EditGrant` is
   deliberately single-target.
2. **Semantic verification is model-judged.** `no_new_unsupported_claims` et al. are LLM checks, so
   they are as good as the model. They are *supplementary*: the deterministic checks are what block
   completion, and the semantic ones add rejection power rather than replacing it.
3. **`max_revision_rounds` interacts with strictness.** With round 1 rejected and a budget of 1, the run
   ends `BLOCKED`. That is intentional, but it means a strict adversarial reviewer plus a low budget
   yields more blocked runs than a team might expect.
4. **No sandboxing of the reviewer's read tools.** Reviewers can read any file under the workspace root
   and search it. On a very large repository that is a cost and information-exposure risk; the
   mitigations are the output caps and the quiet-directory filter, not a filesystem jail.
5. **The scripted-model layer is not a substitute for live evaluation.** It proves the engine's
   behaviour, not that a given model follows the prompts. `REVIEWFLOW_LIVE_TEST=1` exists precisely
   because those are different questions, and the live test is minimal by design.
6. **`review_coverage` counts roles, not depth.** A reviewer that inspected one unit and returned an
   empty report counts as coverage. Nothing in the engine measures "how much of its assignment a
   reviewer actually read" — that is a plausible next hardening step (for example, requiring quotable
   evidence per assigned unit).
