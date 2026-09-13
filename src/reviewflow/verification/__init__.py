"""Verification: deterministic checks, semantic checks and diff evidence."""

from reviewflow.verification.checks import (
    CHECKS,
    DEFAULT_CHECK_NAMES,
    CheckContext,
    CheckSpec,
    register_check,
    run_check,
)
from reviewflow.verification.diff import (
    DiffEvidence,
    WorkspaceSnapshot,
    compute_diff,
    empty_diff,
    snapshot_workspace,
)
from reviewflow.verification.runner import (
    DEFAULT_SEMANTIC_CHECKS,
    SEMANTIC_CHECKS,
    VerificationRunner,
)

__all__ = [
    "CHECKS",
    "DEFAULT_CHECK_NAMES",
    "DEFAULT_SEMANTIC_CHECKS",
    "SEMANTIC_CHECKS",
    "CheckContext",
    "CheckSpec",
    "DiffEvidence",
    "VerificationRunner",
    "WorkspaceSnapshot",
    "compute_diff",
    "empty_diff",
    "register_check",
    "run_check",
    "snapshot_workspace",
]
