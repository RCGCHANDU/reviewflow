"""Specialist registry.

The engine never hard-codes a role. It asks the registry which specialists exist,
what each one is for, and what it is allowed to do. Registering a new specialist
is therefore a data change, not an engine change - and the permission policy
travelling with the spec means a new role cannot accidentally acquire write tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewflow.errors import PermissionViolation
from reviewflow.models.enums import AgentRole, IssueCategory
from reviewflow.permissions import PolicyRegistry, RolePolicy
from reviewflow.prompts.roles import ROLE_SPECS

#: Tools every read-only specialist receives.
READ_TOOLS: tuple[str, ...] = ("read_file", "list_dir", "search_text")
#: Tools the controlled editor receives.
EDITOR_TOOLS: tuple[str, ...] = (*READ_TOOLS, "apply_edit")
#: Agent-as-tool name used by the synthesizer for conflict adjudication.
DEEP_DIVE_TOOL_NAME = "deep_dive"


@dataclass(frozen=True)
class SpecialistSpec:
    """One registrable specialist."""

    role_key: str
    agent_name: str
    description: str
    focus: tuple[IssueCategory, ...] = ()
    #: Mandatory specialists gate coverage: if one fails, the run is degraded.
    mandatory: bool = True
    #: Whether this role may hold mutating tools.
    may_mutate: bool = False
    #: Whether this role appears in the planner's decision-boundary registry.
    selectable_by_planner: bool = True

    def __post_init__(self) -> None:
        if self.may_mutate and self.selectable_by_planner:
            raise PermissionViolation(
                f"{self.role_key!r} is mutating and therefore must not be selectable by the planner"
            )

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(EDITOR_TOOLS if self.may_mutate else READ_TOOLS)

    def policy(self) -> RolePolicy:
        return RolePolicy(
            role_key=self.role_key,
            may_mutate=self.may_mutate,
            allowed_tools=self.allowed_tools,
            may_be_delegated=True,
        )


@dataclass
class SpecialistRegistry:
    """The catalogue of specialists plus their permission policies."""

    specs: dict[str, SpecialistSpec] = field(default_factory=dict)
    policies: PolicyRegistry = field(default_factory=PolicyRegistry)

    def register(self, spec: SpecialistSpec) -> None:
        if spec.role_key in self.specs:
            raise PermissionViolation(f"duplicate specialist registration for {spec.role_key!r}")
        if spec.role_key not in ROLE_SPECS:
            raise PermissionViolation(
                f"specialist {spec.role_key!r} has no prompt specification; add one to prompts/roles.py"
            )
        self.specs[spec.role_key] = spec
        self.policies.register(spec.policy())

    def get(self, role_key: str) -> SpecialistSpec:
        try:
            return self.specs[role_key]
        except KeyError as exc:
            raise PermissionViolation(f"unknown specialist {role_key!r}") from exc

    def keys(self) -> set[str]:
        return set(self.specs)

    def planner_selectable(self) -> list[SpecialistSpec]:
        return [s for s in self.specs.values() if s.selectable_by_planner and not s.may_mutate]

    def reviewers(self) -> list[SpecialistSpec]:
        return [s for s in self.specs.values() if s.selectable_by_planner and not s.may_mutate]

    def mandatory_keys(self) -> list[str]:
        return [s.role_key for s in self.specs.values() if s.mandatory and not s.may_mutate]

    def writers(self) -> list[SpecialistSpec]:
        return [s for s in self.specs.values() if s.may_mutate]

    def writer(self) -> SpecialistSpec:
        writers = self.writers()
        if len(writers) != 1:
            raise PermissionViolation(
                f"exactly one write-capable specialist must be registered, found {len(writers)}"
            )
        return writers[0]

    def describe_for_planner(self) -> str:
        """Registry listing injected into the planner's prompt."""
        lines = []
        for spec in self.planner_selectable():
            focus = ", ".join(c.value for c in spec.focus[:6]) or "general"
            lines.append(f"- {spec.role_key}: {spec.description} Focus: {focus}.")
        return "\n".join(lines)


def build_default_registry() -> SpecialistRegistry:
    """The shipped specialist set.

    Exactly one spec is write-capable (the controlled editor), and it is
    deliberately not planner-selectable: edit authority is granted by the engine.
    """
    from reviewflow.permissions import RolePolicy, register_tool

    # The deep-dive adjudicator is exposed to the synthesizer as an agent-tool; it
    # wraps a read-only agent, so it is registered as non-mutating.
    register_tool(DEEP_DIVE_TOOL_NAME, mutating=False)

    registry = SpecialistRegistry()
    # Internal (non-planner-selectable) roles still need an explicit policy, so that
    # assert_read_only() has something to check them against.
    for role_key, tools in (
        ("orchestrator", {*READ_TOOLS, DEEP_DIVE_TOOL_NAME}),
        ("verifier", set(READ_TOOLS)),
        ("deep_dive_adjudicator", set(READ_TOOLS)),
    ):
        registry.policies.register(
            RolePolicy(
                role_key=role_key,
                may_mutate=False,
                allowed_tools=frozenset(tools),
                may_be_delegated=True,
                notes="internal role: read-only, not offered to the planner",
            )
        )

    for spec in (
        SpecialistSpec(
            role_key=AgentRole.EVIDENCE_AUDITOR.value,
            agent_name="Evidence auditor",
            description="Checks whether every statement is supported by the supplied evidence.",
            focus=(
                IssueCategory.UNSUPPORTED_CLAIM,
                IssueCategory.EVIDENCE_PROVENANCE,
                IssueCategory.QUANTITATIVE_ERROR,
                IssueCategory.SCOPE_MISMATCH,
                IssueCategory.CAUSAL_OVERREACH,
                IssueCategory.MISSING_UNCERTAINTY,
                IssueCategory.INCOMPATIBLE_COMPARISON,
                IssueCategory.FACTUAL_INCONSISTENCY,
            ),
        ),
        SpecialistSpec(
            role_key=AgentRole.RHETORICAL_ANALYST.value,
            agent_name="Rhetorical analyst",
            description="Judges purpose, reasoning structure, clarity, terminology and information flow.",
            focus=(
                IssueCategory.PURPOSE,
                IssueCategory.CLARITY,
                IssueCategory.TERMINOLOGY,
                IssueCategory.INFORMATION_FLOW,
                IssueCategory.REDUNDANCY,
                IssueCategory.LOCAL_COHERENCE,
                IssueCategory.STANCE,
                IssueCategory.AMBIGUITY,
            ),
        ),
        SpecialistSpec(
            role_key=AgentRole.TECHNICAL_REVIEWER.value,
            agent_name="Technical reviewer",
            description="Skeptical domain referee: methodology, assumptions, comparisons, conclusions.",
            focus=(
                IssueCategory.METHODOLOGY,
                IssueCategory.REPRODUCIBILITY,
                IssueCategory.HIDDEN_ASSUMPTION,
                IssueCategory.EVIDENCE_INTERPRETATION,
                IssueCategory.TECHNICAL_LIMITATION,
                IssueCategory.CONCLUSION_WARRANT,
            ),
        ),
        SpecialistSpec(
            role_key=AgentRole.STRUCTURE_REVIEWER.value,
            agent_name="Structure reviewer",
            description="Global coherence: progression, missing components, cross-section contradictions.",
            focus=(
                IssueCategory.LOGICAL_PROGRESSION,
                IssueCategory.MISSING_COMPONENT,
                IssueCategory.CROSS_SECTION_CONTRADICTION,
                IssueCategory.PROMISE_VS_DELIVERY,
                IssueCategory.TERMINOLOGY_CONSISTENCY,
                IssueCategory.GLOBAL_COHERENCE,
            ),
        ),
        SpecialistSpec(
            role_key="diff_reviewer",
            agent_name="Diff reviewer",
            description="Reviews the diff for scope violations and unintended changes.",
            mandatory=False,
            selectable_by_planner=False,
        ),
        SpecialistSpec(
            role_key=AgentRole.ADVERSARIAL_REVIEWER.value,
            agent_name="Adversarial reviewer",
            description="Attempts to falsify the revision after editing.",
            mandatory=False,
            selectable_by_planner=False,
        ),
        SpecialistSpec(
            role_key=AgentRole.CONTROLLED_EDITOR.value,
            agent_name="Controlled editor",
            description="The only write-capable agent; applies the smallest defensible edit.",
            may_mutate=True,
            mandatory=False,
            selectable_by_planner=False,
        ),
    ):
        registry.register(spec)
    return registry
