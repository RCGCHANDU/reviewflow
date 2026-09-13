"""Diffing and workspace snapshots.

The diff is computed from real bytes by the workflow, never taken from an agent's
description of what it did. The workspace snapshot lets the workflow prove that
nothing outside the granted target changed - including that no reviewer mutated
anything.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path

#: Directories never walked when snapshotting a workspace.
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
#: Hard cap on files snapshotted, to keep ingest cheap on large trees.
MAX_SNAPSHOT_FILES = 4000


@dataclass(frozen=True)
class DiffEvidence:
    """Code-computed facts about one edit."""

    path: str
    available: bool
    unified_diff: str
    added_lines: int
    removed_lines: int
    sha_before: str
    sha_after: str
    bytes_before: int
    bytes_after: int

    @property
    def changed(self) -> bool:
        return self.sha_before != self.sha_after

    def stats(self) -> dict[str, object]:
        return {
            "path": self.path,
            "changed": self.changed,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "sha256_before": self.sha_before,
            "sha256_after": self.sha_after,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_diff(path_label: str, before: str, after: str) -> DiffEvidence:
    """Unified diff plus line counts, from the two real revisions."""
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path_label}",
            tofile=f"b/{path_label}",
            lineterm="",
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return DiffEvidence(
        path=path_label,
        available=True,
        unified_diff="\n".join(diff_lines) or "(no textual change)",
        added_lines=added,
        removed_lines=removed,
        sha_before=sha256_bytes(before.encode("utf-8")),
        sha_after=sha256_bytes(after.encode("utf-8")),
        bytes_before=len(before.encode("utf-8")),
        bytes_after=len(after.encode("utf-8")),
    )


def with_disk_hashes(diff: DiffEvidence, *, sha_before: str, sha_after: str) -> DiffEvidence:
    """Return the diff with hashes taken from the file's raw bytes.

    The textual diff is produced from decoded text, which can differ from the bytes
    on disk when a file uses CRLF. Verification compares against the disk, so the
    byte hashes are the authoritative ones.
    """
    return replace(diff, sha_before=sha_before, sha_after=sha_after)


def empty_diff(path_label: str, text: str) -> DiffEvidence:
    """The no-edit case: identical hashes, no hunks."""
    digest = sha256_bytes(text.encode("utf-8"))
    return DiffEvidence(
        path=path_label,
        available=False,
        unified_diff="(no change was applied)",
        added_lines=0,
        removed_lines=0,
        sha_before=digest,
        sha_after=digest,
        bytes_before=len(text.encode("utf-8")),
        bytes_after=len(text.encode("utf-8")),
    )


@dataclass
class WorkspaceSnapshot:
    """SHA-256 of every tracked file at a point in time."""

    root: Path
    hashes: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    def changed_paths(self, other: WorkspaceSnapshot) -> list[str]:
        """Paths whose content differs between two snapshots, either direction."""
        keys = set(self.hashes) | set(other.hashes)
        return sorted(k for k in keys if self.hashes.get(k) != other.hashes.get(k))


def snapshot_workspace(root: Path, *, source_globs: list[str], skip_generated: list[str]) -> WorkspaceSnapshot:
    """Hash the source-of-truth files under ``root``."""
    snapshot = WorkspaceSnapshot(root=root)
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= MAX_SNAPSHOT_FILES:
            snapshot.truncated = True
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if source_globs and not any(fnmatch.fnmatch(relative, g) or fnmatch.fnmatch(path.name, g) for g in source_globs):
            continue
        if any(fnmatch.fnmatch(relative, g) for g in skip_generated):
            continue
        try:
            snapshot.hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:  # pragma: no cover - unreadable file
            continue
        count += 1
    return snapshot
