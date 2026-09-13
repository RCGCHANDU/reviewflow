"""Application error types and their HTTP mapping.

A user of this application should never see "Internal server error" where a precise,
safe explanation exists. Every error carries: what failed, at which stage, which agent or
provider was involved, whether retrying is sensible, and whether the run still has partial
reviewer coverage. Stack traces and secrets stay in the log.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors this application raises deliberately."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "detail": self.detail,
            }
        }

    @property
    def retryable(self) -> bool:
        return False


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_failed"


class UnsafePathError(AppError):
    status_code = 400
    code = "unsafe_path"


class ConflictError(AppError):
    """The request is well-formed but the current state forbids it."""

    status_code = 409
    code = "conflict"


class ProviderUnavailableError(AppError):
    """No credential exists for the provider a run needs.

    Not retryable as-is: the fix is to sign in or reconfigure, which retrying cannot do.
    """

    status_code = 409
    code = "provider_unavailable"


class ExecutionError(AppError):
    """A run could not be started or resumed."""

    status_code = 500
    code = "execution_failed"

    @property
    def retryable(self) -> bool:
        return True
