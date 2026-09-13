"""Ingest: identify the source of truth, snapshot it, and derive planning hints.

Ingest is deterministic. It produces the byte-exact snapshot the workflow is
allowed to edit, plus structural hints that let the planner decompose the target
using the document's real structure instead of an imagined one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reviewflow.config import Settings
from reviewflow.context import WorkflowContext, sha256_of
from reviewflow.errors import ConfigurationError

#: Upper bound on material handed to a single agent call, for cost control.
MAX_MATERIAL_CHARS = 60_000


@dataclass(frozen=True)
class UnitHint:
    """A structural hint used by the planner."""

    location: str
    description: str
    priority: str


@dataclass
class IngestResult:
    """The ingested target snapshot."""

    relative_path: str
    text: str
    sha256: str
    line_count: int
    char_count: int
    truncated: bool = False
    unit_hints: list[UnitHint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def numbered(self, *, max_lines: int | None = None) -> str:
        """Line-numbered material, so reviewers can cite locations precisely."""
        lines = self.text.splitlines()
        if max_lines is not None:
            lines = lines[:max_lines]
        return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(lines, 1))


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_DEF_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)")


class Ingester:
    """Loads and classifies the target artifact."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ingest(self, ctx: WorkflowContext) -> IngestResult:
        target = ctx.target_path
        if not target.is_file():
            raise ConfigurationError(f"target {target} is not a file")
        raw = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw) > MAX_MATERIAL_CHARS
        text = raw[:MAX_MATERIAL_CHARS] if truncated else raw
        relative = target.relative_to(ctx.workspace_root).as_posix()

        result = IngestResult(
            relative_path=relative,
            text=text,
            sha256=sha256_of(target),
            line_count=raw.count("\n") + 1,
            char_count=len(raw),
            truncated=truncated,
            unit_hints=_hints(target, text),
        )
        if result.sha256 != ctx.source_sha256:
            ctx.source_sha256 = result.sha256
        if truncated:
            result.warnings.append(
                f"material truncated to {MAX_MATERIAL_CHARS} characters of {len(raw)}; "
                "reviewers cannot see the remainder and coverage is reported as degraded"
            )
        if target.is_symlink():
            result.warnings.append("target is a symlink; the resolved path is what will be edited")
        return result


def _hints(target: Path, text: str) -> list[UnitHint]:
    """Derive unit hints from the document's own structure."""
    lines = text.splitlines()
    if target.suffix == ".py":
        return _python_hints(lines)
    if target.suffix in {".md", ".rst", ".txt"} or any(_HEADING_RE.match(line) for line in lines[:200]):
        return _heading_hints(lines)
    return _window_hints(lines)


def _heading_hints(lines: list[str]) -> list[UnitHint]:
    hints: list[UnitHint] = []
    current_heading: str | None = None
    current_start = 0
    for index, line in enumerate(lines, 1):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        if current_heading is not None:
            hints.append(
                UnitHint(
                    location=f"L{current_start}-L{index - 1}",
                    description=current_heading,
                    priority="major",
                )
            )
        current_heading = match.group(2)
        current_start = index
    if current_heading is not None:
        hints.append(
            UnitHint(location=f"L{current_start}-L{len(lines)}", description=current_heading, priority="major")
        )
    if not hints:
        return _window_hints(lines)
    # The document's opening block usually carries the purpose and headline claims.
    first = hints[0]
    hints[0] = UnitHint(location=first.location, description=first.description, priority="critical")
    return hints


def _python_hints(lines: list[str]) -> list[UnitHint]:
    hints: list[UnitHint] = []
    current: str | None = None
    start = 0
    for index, line in enumerate(lines, 1):
        match = _DEF_RE.match(line)
        if not match:
            continue
        if current is not None:
            hints.append(UnitHint(location=f"L{start}-L{index - 1}", description=current, priority="major"))
        current = f"definition {match.group(1)}"
        start = index
    if current is not None:
        hints.append(UnitHint(location=f"L{start}-L{len(lines)}", description=current, priority="major"))
    if hints and start > 1:
        hints.insert(0, UnitHint(location=f"L1-L{start - 1}", description="module header and imports", priority="critical"))
    return hints or _window_hints(lines)


def _window_hints(lines: list[str], window: int = 80) -> list[UnitHint]:
    hints: list[UnitHint] = []
    total = max(1, len(lines))
    for start in range(1, total + 1, window):
        end = min(total, start + window - 1)
        hints.append(
            UnitHint(
                location=f"L{start}-L{end}",
                description=f"lines {start} to {end}",
                priority="critical" if start == 1 else "major",
            )
        )
    return hints
