"""Read-only workspace tools for reviewer agents.

Least privilege in practice: reviewers receive only these three tools, all of
which are declared non-mutating in the permission registry. There is no code
path in this module that can write to disk.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from agents import RunContextWrapper
from agents.decorators import tool

from reviewflow.context import WorkflowContext
from reviewflow.errors import PathSecurityError
from reviewflow.permissions import register_tool

#: Hard cap on any single tool result, to keep tokens bounded and prompts small.
MAX_OUTPUT_CHARS = 8_000
#: Hard cap on how much of a file a reviewer may pull in one call.
MAX_LINES_PER_CALL = 400


def resolve_within(root: Path, candidate: str) -> Path:
    """Resolve ``candidate`` against ``root`` and refuse anything that escapes it.

    ``Path.resolve()`` collapses ``..`` and follows symlinks, so a symlink farm
    cannot be used to step outside the workspace either.
    """
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root / raw
    try:
        resolved = joined.expanduser().resolve()
    except OSError as exc:  # pragma: no cover - platform specific
        raise PathSecurityError(f"cannot resolve {candidate!r}: {exc}") from exc
    if not (resolved == root or resolved.is_relative_to(root)):
        raise PathSecurityError(f"path {candidate!r} escapes the workspace root")
    return resolved


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text) - limit} more characters omitted ...]"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


@tool
async def read_file(
    ctx: RunContextWrapper[WorkflowContext],
    path: str,
    start_line: int = 1,
    max_lines: int = MAX_LINES_PER_CALL,
) -> str:
    """Read a text file from the workspace.

    Args:
        path: Path relative to the workspace root, or an absolute path inside it.
        start_line: 1-based first line to return.
        max_lines: Maximum number of lines to return.
    """
    context = ctx.context
    target = resolve_within(context.workspace_root, path)
    if not target.is_file():
        return f"ERROR: {path} is not a readable file"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    window = lines[start - 1 : start - 1 + max(1, max_lines)]
    body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(window))
    header = f"# {_relative(target, context.workspace_root)} (lines {start}-{start + len(window) - 1} of {len(lines)})"
    context.audit("tool_call", tool="read_file", path=_relative(target, context.workspace_root))
    return _truncate(f"{header}\n{body}")


@tool
async def list_dir(ctx: RunContextWrapper[WorkflowContext], path: str = ".") -> str:
    """List the entries of a directory inside the workspace.

    Args:
        path: Directory path relative to the workspace root.
    """
    context = ctx.context
    target = resolve_within(context.workspace_root, path)
    if not target.is_dir():
        return f"ERROR: {path} is not a directory"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))[:200]
    rendered = [f"{'dir ' if e.is_dir() else 'file'} {_relative(e, context.workspace_root)}" for e in entries]
    context.audit("tool_call", tool="list_dir", path=_relative(target, context.workspace_root))
    return _truncate("\n".join(rendered) or "(empty directory)")


@tool
async def search_text(
    ctx: RunContextWrapper[WorkflowContext],
    pattern: str,
    path: str = ".",
    max_results: int = 60,
) -> str:
    """Search for a regular expression across workspace files.

    Args:
        pattern: Python regular expression, matched case-insensitively.
        path: File or directory to search, relative to the workspace root.
        max_results: Maximum number of matches to return.
    """
    context = ctx.context
    target = resolve_within(context.workspace_root, path)
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"ERROR: invalid regular expression: {exc}"

    candidates = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    hits: list[str] = []
    for file in candidates:
        if len(hits) >= max_results:
            break
        if _is_noise(file, context):
            continue
        try:
            for lineno, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{_relative(file, context.workspace_root)}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        break
        except OSError:
            continue
    context.audit("tool_call", tool="search_text", pattern=pattern, hits=len(hits))
    return _truncate("\n".join(hits) or "(no matches)")


def _is_noise(file: Path, context: WorkflowContext) -> bool:
    """Skip generated/vendored trees so searches stay cheap and relevant."""
    relative = _relative(file, context.workspace_root)
    skip_dirs = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
    if any(part in skip_dirs for part in Path(relative).parts):
        return True
    return any(fnmatch.fnmatch(relative, glob) for glob in context.generated_globs)


for _name in ("read_file", "list_dir", "search_text"):
    register_tool(_name, mutating=False)

READ_ONLY_TOOL_NAMES: tuple[str, ...] = ("read_file", "list_dir", "search_text")
