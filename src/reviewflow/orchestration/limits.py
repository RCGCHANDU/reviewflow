"""Bounded retries and cost ceilings.

Retries are keyed on the *class* of failure, never on the error message, and
logical failures are never retried: a reviewer that failed at its task will fail
again, and retrying it would only burn budget and mask the failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from reviewflow.config import Budget, RetryLimits
from reviewflow.models.enums import FailureKind


@dataclass(frozen=True)
class RetryPolicy:
    """Maps failure kinds to bounded retry counts."""

    limits: RetryLimits

    def budget_for(self, kind: FailureKind) -> int:
        match kind:
            case FailureKind.TRANSIENT:
                return self.limits.transient
            case FailureKind.INVALID_OUTPUT:
                return self.limits.invalid_output
            case FailureKind.TIMEOUT:
                return self.limits.timeout
            case FailureKind.LOGICAL:
                return self.limits.logical
            case FailureKind.GUARDRAIL:
                return 0
            case FailureKind.INFRASTRUCTURE:
                return 0
        return 0  # pragma: no cover - exhaustive above

    def can_retry(self, kind: FailureKind, attempts: int) -> bool:
        """``attempts`` is the number of attempts already made."""
        return attempts <= self.budget_for(kind)

    def backoff_seconds(self, kind: FailureKind, attempt: int) -> float:
        """Exponential backoff, capped, for the kinds that are retried."""
        if kind is not FailureKind.TRANSIENT:
            return 0.0
        return min(8.0, 0.5 * (2 ** max(0, attempt - 1)))


class TokenBudget:
    """Tracks token spend against the configured ceiling."""

    def __init__(self, budget: Budget) -> None:
        self.limit = budget.max_total_tokens
        self.spent = 0

    def add(self, tokens: int) -> None:
        self.spent += max(0, tokens)

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)


class RoundBudget:
    """Tracks revision rounds against the configured maximum."""

    def __init__(self, budget: Budget) -> None:
        self.max_rounds = budget.max_revision_rounds
        self.rounds = 0

    def start_round(self) -> int:
        self.rounds += 1
        return self.rounds

    @property
    def can_revise(self) -> bool:
        return self.rounds < self.max_rounds
