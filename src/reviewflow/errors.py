"""Exception hierarchy.

Every failure the workflow can observe is a :class:`ReviewflowError`, so the
engine can catch application failures without catching programming errors.
"""

from __future__ import annotations


class ReviewflowError(Exception):
    """Base class for all reviewflow errors."""


class ConfigurationError(ReviewflowError):
    """The workflow was configured in a way that cannot work (bad path, missing key)."""


class PathSecurityError(ReviewflowError):
    """A path escaped its allowed root, or a protected path was targeted."""


class PermissionViolation(ReviewflowError):
    """A role was granted a capability its policy forbids."""


class GrantError(ReviewflowError):
    """A mutation was attempted without a valid edit grant."""


class IllegalTransition(ReviewflowError):
    """The state machine was asked to perform a transition it does not allow."""


class ContractViolation(ReviewflowError):
    """An agent's structured output violated the contract's semantic rules.

    Distinct from Pydantic's schema validation: the JSON matched the schema but
    the content contradicted the rules the workflow depends on.
    """


class BudgetExceeded(ReviewflowError):
    """A cost ceiling (tokens, rounds, turns) was reached."""


class AgentInvocationError(ReviewflowError):
    """An agent call failed and is not retryable within its budget."""
