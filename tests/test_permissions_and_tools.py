"""Tool security: path handling, grants, and the permission model.

Every test here maps to a guarantee the architecture claims:

* a reviewer cannot write - structurally, and again through a tool guardrail;
* a write needs a grant covering exactly one path at the current hash;
* paths cannot escape the workspace, including through symlinks;
* writes are atomic and byte-exact.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from agents import Agent, Runner
from agents.decorators import tool
from pydantic import BaseModel

from reviewflow.agents import build_default_registry
from reviewflow.agents.factory import AgentFactory
from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.errors import GrantError, PathSecurityError, PermissionViolation
from reviewflow.models.enums import Stage
from reviewflow.orchestration.invoker import RunConfigFactory
from reviewflow.permissions import (
    MUTATING_TOOL_NAMES,
    PolicyRegistry,
    RolePolicy,
    assert_read_only,
    is_mutating,
    tool_names,
)
from reviewflow.simulate import ScriptedModel, ScriptedModelProvider, call, structured
from reviewflow.testkit import call_tool
from reviewflow.tools.edit_tools import _check_authorisation, apply_edit
from reviewflow.tools.fs_tools import list_dir, read_file, resolve_within, search_text


class Out(BaseModel):
    ok: bool


def make_context(workspace, *, grant: bool = True) -> tuple[WorkflowContext, Path]:
    target = workspace.target
    context = WorkflowContext.create(
        objective="test",
        target_path=target,
        workspace_root=workspace.root,
        allowed_write_roots=[workspace.root],
        generated_globs=workspace.settings.generated_globs,
    )
    context.settings = workspace.settings
    context.stage = Stage.CONTROLLED_EDIT
    if grant:
        context.issue_grant(target, sha256_of(target), ["ev-1"], round_index=1)
    return context, target


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


def test_resolve_within_rejects_traversal(workspace) -> None:
    with pytest.raises(PathSecurityError):
        resolve_within(workspace.root, "../outside.txt")
    with pytest.raises(PathSecurityError):
        resolve_within(workspace.root, str(workspace.root.parent / "elsewhere.txt"))


def test_resolve_within_rejects_symlink_escape(workspace, tmp_path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace.root / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges on Windows
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(PathSecurityError):
        resolve_within(workspace.root, "link.txt")


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_tools_run_through_the_sdk_pipeline(workspace) -> None:
    context, _ = make_context(workspace, grant=False)
    for name in ("read_file", "list_dir", "search_text"):
        assert is_mutating(name) is False

    listing = await call_tool(list_dir, context, path=".")
    assert "manuscript.md" in listing
    content = await call_tool(read_file, context, path="manuscript.md")
    assert "97.3" in content and "|" in content, "content should be line-numbered for precise citation"
    hits = await call_tool(search_text, context, pattern="97.3", path=".")
    assert "manuscript.md:1" in hits or "manuscript.md:" in hits


@pytest.mark.asyncio
async def test_read_tool_cannot_escape_the_workspace(workspace) -> None:
    """Through the SDK pipeline a denial is returned to the model as text."""
    context, _ = make_context(workspace, grant=False)
    result = await call_tool(read_file, context, path="../outside.txt")
    assert "escapes the workspace root" in result


# --------------------------------------------------------------------------- #
# Permission model
# --------------------------------------------------------------------------- #


def test_read_only_assertion_blocks_a_write_tool() -> None:
    registry = PolicyRegistry()
    registry.register(RolePolicy(role_key="reviewer", may_mutate=False, allowed_tools=frozenset({"read_file"})))
    agent = Agent(name="sneaky", tools=[read_file, apply_edit])
    with pytest.raises(PermissionViolation) as excinfo:
        assert_read_only(agent, "reviewer", registry)
    assert "must not hold mutating tools" in str(excinfo.value)


def test_unregistered_tool_fails_closed() -> None:
    registry = PolicyRegistry()
    registry.register(RolePolicy(role_key="reviewer", may_mutate=False))  # empty allow-list

    @tool
    def brand_new_tool(x: str) -> str:
        """A tool nobody registered."""
        return x

    with pytest.raises(PermissionViolation) as excinfo:
        assert_read_only(Agent(name="a", tools=[brand_new_tool]), "reviewer", registry)
    assert "unregistered tool" in str(excinfo.value)


def test_policy_allow_list_rejects_unexpected_tools() -> None:
    registry = PolicyRegistry()
    registry.register(RolePolicy(role_key="reviewer", may_mutate=False, allowed_tools=frozenset({"read_file"})))
    with pytest.raises(PermissionViolation):
        assert_read_only(Agent(name="a", tools=[read_file, search_text]), "reviewer", registry)


def test_unknown_role_has_no_policy() -> None:
    registry = PolicyRegistry()
    with pytest.raises(PermissionViolation):
        assert_read_only(Agent(name="a"), "ghost", registry)


def test_editor_policy_permits_the_write_tool() -> None:
    registry = build_default_registry()
    spec = registry.writer()
    policy = registry.policies.get(spec.role_key)
    assert policy.may_mutate
    assert "apply_edit" in policy.allowed_tools
    policy.check(Agent(name="editor", tools=[read_file, apply_edit]))


def test_build_specialist_refuses_the_write_role() -> None:
    factory = AgentFactory(Settings().resolved(), build_default_registry())
    with pytest.raises(AssertionError):
        factory.build_specialist("controlled_editor")


def test_tool_names_helper_and_mutating_registry() -> None:
    assert set(tool_names(Agent(name="a", tools=[read_file, search_text]))) == {"read_file", "search_text"}
    assert frozenset({"apply_edit"}) == MUTATING_TOOL_NAMES
    assert is_mutating("apply_edit") is True
    assert is_mutating("nope-not-a-tool") is None


# --------------------------------------------------------------------------- #
# apply_edit authorisation
# --------------------------------------------------------------------------- #


def test_authorisation_check_raises_directly(workspace) -> None:
    """Defence in depth: the shared authorisation helper raises, it does not return a flag."""
    context, target = make_context(workspace, grant=False)
    with pytest.raises(GrantError):
        _check_authorisation(context, "manuscript.md", ["ev-1"])
    context.issue_grant(target, sha256_of(target), ["ev-1"], round_index=1)
    with pytest.raises(GrantError):
        _check_authorisation(context, "manuscript.md", ["ev-999"])
    with pytest.raises(PathSecurityError):
        _check_authorisation(context, "../outside.md", ["ev-1"])
    target.write_bytes(target.read_bytes() + b"changed\n")
    with pytest.raises(GrantError):
        _check_authorisation(context, "manuscript.md", ["ev-1"])


@pytest.mark.asyncio
async def test_apply_edit_requires_a_grant(workspace) -> None:
    context, _ = make_context(workspace, grant=False)
    result = await call_tool(
        apply_edit, context, path="manuscript.md", old_text="our", new_text="x", issue_ids=["ev-1"], rationale="r"
    )
    assert "no active edit grant" in result


@pytest.mark.asyncio
async def test_guardrail_reports_a_denial_before_the_body_runs(workspace) -> None:
    """A denied call is refused by the SDK tool guardrail, and the file stays untouched."""
    context, _ = make_context(workspace, grant=False)
    result = await call_tool(
        apply_edit, context, path="manuscript.md", old_text="our", new_text="x", issue_ids=["ev-1"], rationale="r"
    )
    assert "denied" in result or "no active edit grant" in result
    assert "97.3" in workspace.text()


@pytest.mark.asyncio
async def test_apply_edit_rejects_another_path(workspace) -> None:
    (workspace.root / "other.md").write_text("other\n", encoding="utf-8")
    context, _ = make_context(workspace)
    result = await call_tool(
        apply_edit, context, path="other.md", old_text="other", new_text="x", issue_ids=["ev-1"], rationale="r"
    )
    assert "not the granted target" in result
    assert (workspace.root / "other.md").read_text(encoding="utf-8") == "other\n"


@pytest.mark.asyncio
async def test_apply_edit_rejects_stale_hash(workspace) -> None:
    context, target = make_context(workspace)
    target.write_bytes(target.read_bytes() + b"outside change\n")
    result = await call_tool(
        apply_edit, context, path="manuscript.md", old_text="our", new_text="x", issue_ids=["ev-1"], rationale="r"
    )
    assert "changed since the grant" in result


@pytest.mark.asyncio
async def test_apply_edit_rejects_issue_ids_outside_the_grant(workspace) -> None:
    context, _ = make_context(workspace)
    result = await call_tool(
        apply_edit, context, path="manuscript.md", old_text="our", new_text="x", issue_ids=["ev-999"], rationale="r"
    )
    assert "outside the grant" in result


@pytest.mark.asyncio
async def test_apply_edit_requires_a_unique_quote(workspace) -> None:
    context, _ = make_context(workspace)
    result = json.loads(
        await call_tool(apply_edit, context, path="manuscript.md", old_text="e", new_text="E", issue_ids=["ev-1"], rationale="r")
    )
    assert result["status"] == "rejected"
    assert "occurs" in result["error"] or "not found" in result["error"]


@pytest.mark.asyncio
async def test_apply_edit_refuses_generated_artifacts(workspace) -> None:
    generated = workspace.root / "artifacts" / "report.generated.md"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("generated\n", encoding="utf-8")
    context, _ = make_context(workspace)
    context.edit_grant = None
    context.issue_grant(generated, sha256_of(generated), ["ev-1"], round_index=1)
    result = await call_tool(
        apply_edit,
        context,
        path="artifacts/report.generated.md",
        old_text="generated",
        new_text="x",
        issue_ids=["ev-1"],
        rationale="r",
    )
    assert "generated artifact" in result
    assert generated.read_text(encoding="utf-8") == "generated\n"


@pytest.mark.asyncio
async def test_apply_edit_refuses_python_that_would_not_parse(workspace, make_workspace) -> None:
    ws = make_workspace("def good():\n    return 1\n", filename="mod.py")
    context, _ = make_context(ws)
    result = json.loads(
        await call_tool(apply_edit, context, path="mod.py", old_text="return 1", new_text="return (", issue_ids=["ev-1"], rationale="r")
    )
    assert result["status"] == "rejected"
    assert "parse" in result["error"]


@pytest.mark.asyncio
async def test_apply_edit_is_byte_exact_and_atomic(workspace) -> None:
    """A write must not translate line endings, and no temp file may be left behind."""
    crlf = workspace.root / "crlf.md"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    context, _ = make_context(workspace)
    context.edit_grant = None
    context.issue_grant(crlf, sha256_of(crlf), ["ev-1"], round_index=1)

    receipt = json.loads(
        await call_tool(apply_edit, context, path="crlf.md", old_text="line two", new_text="line 2", issue_ids=["ev-1"], rationale="r")
    )
    assert receipt["status"] == "applied"
    assert crlf.read_bytes() == b"line one\r\nline 2\r\n", "line endings must survive byte for byte"
    assert not list(workspace.root.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_sequential_edits_in_one_run_are_allowed(workspace) -> None:
    """The grant advances with its own writes; an outside change is still refused."""
    context, target = make_context(workspace)
    first = json.loads(
        await call_tool(apply_edit, context, path="manuscript.md", old_text="97.3", new_text="97.4", issue_ids=["ev-1"], rationale="r")
    )
    second = json.loads(
        await call_tool(apply_edit, context, path="manuscript.md", old_text="97.4", new_text="97.5", issue_ids=["ev-1"], rationale="r")
    )
    assert first["status"] == second["status"] == "applied"
    assert "97.5" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_line_budget_is_enforced(workspace) -> None:
    context, _ = make_context(workspace)
    context.edit_grant = replace(context.require_grant(), max_changed_lines=1)
    big = "\n".join(f"line {i}" for i in range(20))
    result = json.loads(
        await call_tool(apply_edit, context, path="manuscript.md", old_text="# Sparse Retrieval", new_text=big, issue_ids=["ev-1"], rationale="r")
    )
    assert result["status"] == "rejected"
    assert "budget" in result["error"]


# --------------------------------------------------------------------------- #
# The SDK tool guardrail is a second, independent gate
# --------------------------------------------------------------------------- #


async def _run_tool_call(workspace, context, tool_call, *, tools=None):
    model = ScriptedModel({"controlled_editor": [tool_call, structured({"ok": True})]})
    factory = RunConfigFactory(
        workspace.settings, workflow_id=context.workflow_id, model_provider=ScriptedModelProvider(model)
    )
    agent: Agent = Agent(
        name="editor",
        instructions="role=controlled_editor agent=controlled_editor stage=controlled_edit round=1",
        tools=tools or [read_file, apply_edit],
        model="gpt-5.4-mini",
    )
    return await Runner.run(
        agent,
        "edit it",
        context=context,
        max_turns=4,
        run_config=factory.build(stage=Stage.CONTROLLED_EDIT, role_key="controlled_editor", round_index=1),
    )


@pytest.mark.asyncio
async def test_tool_guardrail_denies_a_write_without_a_grant(workspace) -> None:
    context, _ = make_context(workspace, grant=False)
    result = await _run_tool_call(
        workspace,
        context,
        call("apply_edit", path="manuscript.md", old_text="97.3", new_text="1", issue_ids=["ev-1"], rationale="r"),
    )
    outputs = [item.output for item in result.new_items if getattr(item, "type", "") == "tool_call_output_item"]
    assert outputs and "denied" in str(outputs[0])
    assert "97.3" in workspace.text(), "the file must be untouched"


@pytest.mark.asyncio
async def test_tool_guardrail_denies_a_path_outside_the_grant(workspace) -> None:
    (workspace.root / "other.md").write_text("other\n", encoding="utf-8")
    context, _ = make_context(workspace)
    result = await _run_tool_call(
        workspace,
        context,
        call("apply_edit", path="other.md", old_text="other", new_text="x", issue_ids=["ev-1"], rationale="r"),
    )
    outputs = [item.output for item in result.new_items if getattr(item, "type", "") == "tool_call_output_item"]
    assert outputs and "not the granted target" in str(outputs[0])
    assert (workspace.root / "other.md").read_text(encoding="utf-8") == "other\n"
