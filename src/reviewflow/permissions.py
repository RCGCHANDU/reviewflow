"""Tool permission model: least privilege enforced in code, not in prompts.

Two independent mechanisms:

1. **Structural** - a read-only role's ``Agent`` is built without any mutating
   tool. :func:`assert_read_only` re-checks the assembled ``Agent.tools`` list
   and fails closed on any tool whose mutability is unknown.
2. **Authorisation** - the mutating tool itself refuses to act without a grant
   (see :mod:`reviewflow.tools.edit_tools`), and an SDK tool input guardrail
   repeats the path/scope check before execution.

Unregistered tools are treated as mutating-unknown and therefore forbidden for
read-only roles. Adding a tool to an agent without registering it here is a
fail-closed error, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from reviewflow.errors import PermissionViolation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agents import Agent

#: Tool name -> whether it can mutate the workspace.
TOOL_MUTABILITY: dict[str, bool] = {}


def register_tool(name: str, *, mutating: bool) -> None:
    """Declare a tool's capability. Called at import time by the tool modules."""
    TOOL_MUTABILITY[name] = mutating


def is_mutating(tool_name: str) -> bool | None:
    """Return the declared mutability, or ``None`` when the tool is unregistered."""
    return TOOL_MUTABILITY.get(tool_name)


def tool_names(agent: Agent[Any]) -> list[str]:
    """Names of the tools attached to an agent, using the SDK's public attribute."""
    names: list[str] = []
    for tool in getattr(agent, "tools", []) or []:
        name = getattr(tool, "name", None)
        if isinstance(name, str):
            names.append(name)
    return names


@dataclass(frozen=True)
class RolePolicy:
    """What one role is allowed to do."""

    role_key: str
    #: Whether this role may hold mutating tools at all.
    may_mutate: bool
    #: Tools the role is expected to hold; anything else is a configuration error.
    allowed_tools: frozenset[str] = frozenset()
    #: Whether the role's agent may be offered as a tool to another agent.
    may_be_delegated: bool = False
    notes: str = ""

    def check(self, agent: Agent[Any]) -> None:
        """Raise :class:`PermissionViolation` if the agent's tools breach this policy."""
        present = tool_names(agent)
        if not self.may_mutate:
            offending = []
            for name in present:
                mutating = is_mutating(name)
                if mutating is None:
                    raise PermissionViolation(
                        f"read-only role {self.role_key!r} holds unregistered tool {name!r}; "
                        "register the tool's mutability before attaching it"
                    )
                if mutating:
                    offending.append(name)
            if offending:
                raise PermissionViolation(
                    f"read-only role {self.role_key!r} must not hold mutating tools: {sorted(offending)}"
                )
        unexpected = sorted(set(present) - set(self.allowed_tools)) if self.allowed_tools else []
        if unexpected:
            raise PermissionViolation(
                f"role {self.role_key!r} holds tools outside its policy: {unexpected}; "
                f"allowed: {sorted(self.allowed_tools)}"
            )


@dataclass
class PolicyRegistry:
    """The set of role policies, plus the fail-closed assertion used by the engine."""

    policies: dict[str, RolePolicy] = field(default_factory=dict)

    def register(self, policy: RolePolicy) -> None:
        if policy.role_key in self.policies:
            raise PermissionViolation(f"duplicate policy for role {policy.role_key!r}")
        self.policies[policy.role_key] = policy

    def get(self, role_key: str) -> RolePolicy:
        try:
            return self.policies[role_key]
        except KeyError as exc:
            raise PermissionViolation(f"no permission policy registered for role {role_key!r}") from exc

    def writers(self) -> list[str]:
        return sorted(k for k, p in self.policies.items() if p.may_mutate)


def assert_read_only(agent: Agent[Any], role_key: str, registry: PolicyRegistry) -> None:
    """Convenience wrapper used at agent-build time."""
    registry.get(role_key).check(agent)


#: Tools that can change the workspace. Registered by :mod:`reviewflow.tools.edit_tools`.
MUTATING_TOOL_NAMES: frozenset[str] = frozenset({"apply_edit"})
