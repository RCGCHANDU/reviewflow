"""Agent construction.

Every agent is built here, and every build ends with a permission assertion, so
an agent that would violate its role's policy cannot exist long enough to run.
Instructions are *dynamic* (a callable the SDK invokes per run) so the same agent
object reports its current stage and round without rebuilding prompts by hand.
"""

from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings, RunContextWrapper, Tool
from agents.decorators import tool

from reviewflow.agents.registry import DEEP_DIVE_TOOL_NAME, SpecialistRegistry
from reviewflow.config import Settings
from reviewflow.context import WorkflowContext
from reviewflow.guardrails.final_status import final_status_guardrail
from reviewflow.guardrails.task_scope import task_scope_guardrail
from reviewflow.models.contracts import (
    Adjudication,
    AdversarialVerdict,
    ConsolidatedIssueMap,
    DiffReviewResult,
    EditResult,
    FinalReport,
    ReviewerReport,
    ReviewPlan,
    VerificationBatch,
)
from reviewflow.permissions import assert_read_only, tool_names
from reviewflow.prompts import (
    ROLE_SPECS,
    RolePromptSpec,
    compose_role_prompt,
    delivery_instructions,
    render_output_contract,
)
from reviewflow.tools.edit_tools import apply_edit
from reviewflow.tools.fs_tools import list_dir, read_file, search_text

#: Typed as the SDK's ``Tool`` union: ``Agent.tools`` is a ``list[Tool]`` and
#: ``list`` is invariant, so annotating with the concrete ``FunctionTool`` type
#: would be rejected by the type checker even though it is correct at runtime.
READ_TOOLS: list[Tool] = [read_file, list_dir, search_text]
README_TOOLS: list[Tool] = [*READ_TOOLS, apply_edit]


#: Agents whose model deliberately comes from a different tier than their own role key
#: implies. Kept as data (not as an if-branch in the builder) so that `reviewflow roles`
#: and the factory cannot disagree about which model an agent runs on.
MODEL_TIER_OVERRIDES: dict[str, str] = {
    # The adjudicator settles conflicts on the orchestrator's model, so the two do not
    # disagree about what counts as settled.
    "deep_dive_adjudicator": "orchestrator",
}


class AgentFactory:
    """Builds the SDK agents for one configuration.

    ``router`` is the optional provider seam. When it is present, role models and
    provider-mandated model settings (the Codex backend's ``store=false``) come from
    the routed provider; when it is absent - the scripted-test path - nothing changes.
    """

    def __init__(self, settings: Settings, registry: SpecialistRegistry, router: Any = None) -> None:
        self.settings = settings
        self.registry = registry
        self.router = router

    # -- instruction composition ------------------------------------------- #

    def _instructions(self, role_key: str, output_model: type | None, agent_key: str | None = None) -> Any:
        spec: RolePromptSpec = ROLE_SPECS[role_key]
        contract = render_output_contract(output_model) if output_model is not None else ""
        agent_name = agent_key or role_key

        def build(ctx: RunContextWrapper[WorkflowContext], agent: Agent[WorkflowContext]) -> str:
            stage = ctx.context.stage.value if ctx.context.stage else "unknown"
            round_index = ctx.context.round_index
            runtime = delivery_instructions(
                role=role_key, agent=agent_name, stage=stage, round_index=round_index, attempt=1
            )
            if role_key == "orchestrator" and stage == "plan":
                runtime = f"{runtime}\n\nAvailable specialists:\n{self.registry.describe_for_planner()}"
            if role_key == "controlled_editor":
                grant = ctx.context.edit_grant
                if grant is not None:
                    target = grant.target_path.relative_to(ctx.context.workspace_root).as_posix()
                    runtime = (
                        f"{runtime}\n\nGRANTED TARGET (the only file you may modify): {target}\n"
                        f"grant_id={grant.grant_id} round={grant.round_index}\n"
                        f"line budget for this change: {grant.max_changed_lines}"
                    )
            return compose_role_prompt(spec, output_contract=contract, runtime_context=runtime)

        return build

    def model_for(self, role_key: str) -> str:
        """Public view of the model a role will actually be built with.

        Diagnostics (`reviewflow roles`) read this rather than re-deriving the routing
        rule, because a reporting path that re-implements resolution is a reporting path
        that can disagree with what runs - and a wrong answer from a diagnostic tool is
        worse than no tool.
        """
        return self._model(role_key)

    def _model(self, role_key: str) -> str:
        """Resolve the model string for a role, through the router when one is set."""
        role_key = MODEL_TIER_OVERRIDES.get(role_key, role_key)
        spec = self.registry.specs.get(role_key)
        write_capable = bool(spec and spec.may_mutate)
        adversarial = role_key == "adversarial_reviewer"
        configured = self.settings.models.for_role(
            role_key, write_capable=write_capable, adversarial=adversarial
        )
        if self.router is None:
            return configured
        return str(
            self.router.model_for_role(
                role_key,
                write_capable=write_capable,
                adversarial=adversarial,
                configured=configured,
            )
        )

    def _model_settings(self, agent_key: str, role_key: str, model_name: str | None = None) -> ModelSettings:
        """Per-agent model tuning, empty unless configured or provider-mandated.

        A provider may *require* settings (the Codex backend rejects ``store=true``),
        so the router gets the last word on those, while explicit per-agent overrides
        still win for everything the provider does not mandate.
        """
        overrides = self.settings.settings_for_agent(agent_key, role_key)
        base = ModelSettings(**overrides) if overrides else ModelSettings()
        if self.router is None or model_name is None:
            return base
        return self.router.model_settings_for(model_name, base)


    # -- builders ---------------------------------------------------------- #

    def build_specialist(self, role_key: str) -> Agent[WorkflowContext]:
        """Build a read-only reviewer. Raises if the role is write-capable."""
        spec = self.registry.get(role_key)
        if spec.may_mutate:
            raise AssertionError(f"{role_key} is write-capable; use build_editor()")
        model_name = self._model(role_key)
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name=spec.agent_name,
            instructions=self._instructions(role_key, ReviewerReport, spec.role_key),
            model=model_name,
            model_settings=self._model_settings(role_key, role_key, model_name),
            tools=READ_TOOLS,
            output_type=ReviewerReport,
        )
        assert_read_only(agent, role_key, self.registry.policies)
        return agent

    def build_planner(self) -> Agent[WorkflowContext]:
        model_name = self._model("orchestrator")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Lead orchestrator (planner)",
            instructions=self._instructions("orchestrator", ReviewPlan, "planner"),
            model=model_name,
            model_settings=self._model_settings("planner", "orchestrator", model_name),
            tools=READ_TOOLS,
            output_type=ReviewPlan,
            input_guardrails=[task_scope_guardrail],
        )
        assert_read_only(agent, "orchestrator", self.registry.policies)
        return agent

    def build_synthesizer(self) -> Agent[WorkflowContext]:
        """The orchestrator's synthesis stage.

        This is the workflow's only ``Agent.as_tool()`` relationship: the decision
        of *which* unresolved conflict deserves a deep dive is semantic, and the
        orchestrator must keep ownership of the final issue map, so the adjudicator
        is exposed as a tool rather than as a handoff (which would transfer the
        conversation away and destroy the reviewer-independence story).
        """
        adjudicator: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Deep-dive adjudicator",
            instructions=self._instructions("deep_dive_adjudicator", Adjudication, "deep_dive_adjudicator"),
            model=self._model("deep_dive_adjudicator"),  # tier override lives in MODEL_TIER_OVERRIDES
            tools=READ_TOOLS,
            output_type=Adjudication,
        )
        assert_read_only(adjudicator, "orchestrator", self.registry.policies)

        @tool(name_override=DEEP_DIVE_TOOL_NAME)
        async def deep_dive(ctx: RunContextWrapper[WorkflowContext], question: str) -> str:
            """Consult the adjudicator on one specific unresolved conflict.

            Args:
                question: The single question to settle, naming the two propositions
                    being weighed and the locations involved.
            """
            from reviewflow.guardrails.task_scope import settings_run_config
            from reviewflow.orchestration.invoker import execute_run, requires_streaming_for

            provider = ctx.context.model_provider
            result = await execute_run(
                adjudicator,
                question,
                context=ctx.context,
                max_turns=min(4, self.settings.budget.max_turns_per_agent),
                run_config=settings_run_config(self.settings, provider=provider),
                requires_streaming=requires_streaming_for(provider, adjudicator.model),
            )
            outcome: Adjudication = result.final_output
            # Usage of the nested run is aggregated by the SDK into the parent run's
            # usage, so it is deliberately not recorded again here.
            return outcome.model_dump_json()

        model_name = self._model("orchestrator")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Lead orchestrator (synthesizer)",
            instructions=self._instructions("orchestrator", ConsolidatedIssueMap, "synthesizer"),
            model=model_name,
            model_settings=self._model_settings("synthesizer", "orchestrator", model_name),
            tools=[*READ_TOOLS, deep_dive],
            output_type=ConsolidatedIssueMap,
        )
        assert_read_only(agent, "orchestrator", self.registry.policies)
        assert DEEP_DIVE_TOOL_NAME in tool_names(agent)
        return agent

    def build_editor(self) -> Agent[WorkflowContext]:
        role_key = self.registry.writer().role_key
        model_name = self._model(role_key)
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name=self.registry.writer().agent_name,
            instructions=self._instructions(role_key, EditResult, "controlled_editor"),
            model=model_name,
            model_settings=self._model_settings("controlled_editor", role_key, model_name),
            tools=README_TOOLS,
            output_type=EditResult,
        )
        self.registry.policies.get(role_key).check(agent)
        return agent

    def build_diff_reviewer(self) -> Agent[WorkflowContext]:
        model_name = self._model("diff_reviewer")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Diff reviewer",
            instructions=self._instructions("diff_reviewer", DiffReviewResult, "diff_reviewer"),
            model=model_name,
            model_settings=self._model_settings("diff_reviewer", "diff_reviewer", model_name),
            tools=READ_TOOLS,
            output_type=DiffReviewResult,
        )
        assert_read_only(agent, "diff_reviewer", self.registry.policies)
        return agent

    def build_adversarial_reviewer(self) -> Agent[WorkflowContext]:
        model_name = self._model("adversarial_reviewer")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Adversarial reviewer",
            instructions=self._instructions("adversarial_reviewer", AdversarialVerdict, "adversarial_reviewer"),
            model=model_name,
            model_settings=self._model_settings("adversarial_reviewer", "adversarial_reviewer", model_name),
            tools=READ_TOOLS,
            output_type=AdversarialVerdict,
        )
        assert_read_only(agent, "adversarial_reviewer", self.registry.policies)
        return agent

    def build_verifier(self) -> Agent[WorkflowContext]:
        model_name = self._model("verifier")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Verifier",
            instructions=self._instructions("verifier", VerificationBatch, "verifier"),
            model=model_name,
            model_settings=self._model_settings("verifier", "verifier", model_name),
            tools=READ_TOOLS,
            output_type=VerificationBatch,
        )
        assert_read_only(agent, "verifier", self.registry.policies)
        return agent

    def build_final_reporter(self) -> Agent[WorkflowContext]:
        """The only agent carrying an output guardrail: it produces the final answer.

        SDK output guardrails run on the final-output agent, which is exactly here,
        so the status-claim check is attached where its semantics are meaningful.
        """
        model_name = self._model("orchestrator")
        agent: Agent[WorkflowContext] = Agent[WorkflowContext](
            name="Lead orchestrator (final report)",
            instructions=self._instructions("orchestrator", FinalReport, "final_reporter"),
            model=model_name,
            model_settings=self._model_settings("final_reporter", "orchestrator", model_name),
            tools=READ_TOOLS,
            output_type=FinalReport,
            output_guardrails=[final_status_guardrail],
        )
        assert_read_only(agent, "orchestrator", self.registry.policies)
        return agent
