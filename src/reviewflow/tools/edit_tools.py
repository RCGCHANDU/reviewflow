"""The single mutating tool in the system.

``apply_edit`` is the only code path that can change the workspace, and it
refuses to run unless *all* of the following hold:

1. the caller's context carries a live :class:`~reviewflow.context.EditGrant`
   (application-level authorisation, minted by the engine, never by a model);
2. the grant covers exactly this path, and the path is inside an allowed root;
3. the file's SHA-256 still matches the grant's snapshot (optimistic locking);
4. the cited issue ids are inside the grant;
5. the file is not matched by a generated-artifact glob;
6. the replaced text occurs exactly once (no ambiguous edits);
7. the change stays inside the grant's line budget;
8. the result still parses (``.py`` targets are compiled before writing);
9. the write is atomic (temp file + ``os.replace``) so a crash cannot truncate
   the artifact.

An SDK tool input guardrail repeats checks 1-5 before execution, so a forbidden
call is refused by the SDK's tool pipeline as well as by the tool body.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import tempfile
from pathlib import Path

from agents import RunContextWrapper
from agents.decorators import tool, tool_input_guardrail
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolInputGuardrailData

from reviewflow.context import EditGrant, WorkflowContext, sha256_of
from reviewflow.errors import GrantError, PathSecurityError
from reviewflow.permissions import register_tool
from reviewflow.tools.fs_tools import resolve_within


def _check_authorisation(
    context: WorkflowContext, path: str, issue_ids: list[str] | None = None
) -> tuple[Path, EditGrant]:
    """Raise unless the mutation is authorised. Shared by the guardrail and the tool body."""
    grant = context.require_grant()
    target = resolve_within(context.workspace_root, path)
    if not grant.covers(target):
        raise GrantError(f"{path} is not the granted target for round {grant.round_index}")
    relative = target.relative_to(context.workspace_root).as_posix()
    for glob in context.generated_globs:
        if fnmatch.fnmatch(relative, glob) or fnmatch.fnmatch(target.name, glob):
            raise PathSecurityError(f"{relative} is a generated artifact and must not be edited")
    # Unconditional: an edit citing nothing is not a check that passes, it is an edit that
    # has not justified itself. The `if issue_ids:` guard that used to be here skipped the
    # check entirely for an empty list.
    grant.check_issues(list(issue_ids or []))
    if target.exists():
        grant.check_hash(sha256_of(target))
    return target, grant


@tool_input_guardrail
def authorize_edit_path(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """SDK-level pre-execution check for :func:`apply_edit`.

    Runs before the tool body. It can only read the tool arguments and the run
    context, which is exactly what a pre-execution authorisation needs.
    """
    context = getattr(data.context, "context", None)
    # data.context is the *tool* context, so the app context is nested one level down.
    if context is None or not isinstance(context, WorkflowContext):
        return ToolGuardrailFunctionOutput.reject_content("edit denied: no application context")
    try:
        args = json.loads(data.context.tool_arguments or "{}")
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput.reject_content("edit denied: unparseable arguments")
    try:
        _check_authorisation(context, str(args.get("path", "")), list(args.get("issue_ids") or []))
    except (GrantError, PathSecurityError) as exc:
        context.audit("guardrail_blocked", tool="apply_edit", reason=str(exc))
        return ToolGuardrailFunctionOutput.reject_content(f"edit denied: {exc}")
    return ToolGuardrailFunctionOutput.allow()


@tool(tool_input_guardrails=[authorize_edit_path])
async def apply_edit(
    ctx: RunContextWrapper[WorkflowContext],
    path: str,
    old_text: str,
    new_text: str,
    issue_ids: list[str],
    rationale: str,
) -> str:
    """Replace one exact passage in the granted target file.

    This is the only tool that can modify the workspace. Use it once per change.
    ``old_text`` must appear exactly once in the file and must be quoted verbatim
    from the current contents, including whitespace.

    Args:
        path: The granted target path, relative to the workspace root.
        old_text: Exact existing text to replace (must occur exactly once).
        new_text: Replacement text. Use an empty string to delete a passage.
        issue_ids: Accepted issue ids from the consolidated issue map that this
            change resolves. Ids outside the map are rejected.
        rationale: Why this is the smallest defensible fix.
    """
    context = ctx.context
    target, grant = _check_authorisation(context, path, issue_ids)

    if not target.is_file():
        return _error("target is not an existing file; this tool edits, it does not create")
    if not old_text:
        return _error("old_text must be a non-empty verbatim quote of existing text")

    # Byte-exact I/O: text mode translates line endings on Windows, which would
    # change the file's hash - the very thing the grant and verification rely on.
    original_bytes = target.read_bytes()
    original = original_bytes.decode("utf-8")
    occurrences = original.count(old_text)
    if occurrences == 0:
        return _error(
            "old_text was not found in the file. Quote it verbatim from the current contents, without the "
            "line-number prefixes, and remember that a phrase wrapped across two file lines contains a newline."
        )
    if occurrences > 1:
        return _error(f"old_text occurs {occurrences} times; include more surrounding context to make it unique")

    updated = original.replace(old_text, new_text, 1)
    changed_lines = _changed_line_count(original, updated)
    if changed_lines > grant.max_changed_lines:
        return _error(
            f"edit changes {changed_lines} lines which exceeds the granted budget "
            f"of {grant.max_changed_lines}; split the work or narrow the change"
        )
    if target.suffix == ".py":
        try:
            compile(updated, str(target), "exec")
        except SyntaxError as exc:
            return _error(f"edit rejected: the result would not parse as Python ({exc})")

    sha_before = sha256_of(target)
    grant.check_hash(sha_before)  # re-check immediately before writing
    _atomic_write(target, updated.encode("utf-8"))
    sha_after = sha256_of(target)

    added, removed = _line_delta(original, updated)
    receipt = {
        "status": "applied",
        "path": target.relative_to(context.workspace_root).as_posix(),
        "sha256_before": sha_before,
        "sha256_after": sha_after,
        "lines_changed": changed_lines,
        "lines_added": added,
        "lines_removed": removed,
        "issue_ids": issue_ids,
        "rationale": rationale,
        "bytes_before": len(original.encode("utf-8")),
        "bytes_after": len(updated.encode("utf-8")),
    }
    # The authorised writer may make further edits in the same run; any change made
    # by anything else still fails the hash check.
    context.advance_grant_hash(sha_after)
    context.audit("mutation", **receipt)
    return json.dumps(receipt)


def _error(message: str) -> str:
    return json.dumps({"status": "rejected", "error": message})


def _line_delta(before: str, after: str) -> tuple[int, int]:
    """Count added and removed lines between two revisions."""
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
    added = removed = 0
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _changed_line_count(before: str, after: str) -> int:
    added, removed = _line_delta(before, after)
    return added + removed



def _atomic_write(target: Path, payload: bytes) -> None:
    """Write bytes via a sibling temp file plus ``os.replace``.

    Binary mode plus atomic replacement means readers never see a partial file, and
    the bytes on disk are exactly the bytes that were hashed.
    """
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


register_tool("apply_edit", mutating=True)
MUTATING_TOOL_NAMES: tuple[str, ...] = ("apply_edit",)
