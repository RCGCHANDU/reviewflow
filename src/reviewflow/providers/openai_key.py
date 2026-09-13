"""Plain OpenAI API-key credentials.

The original path: an ``OPENAI_API_KEY`` in the environment, optionally pointed at
a compatible base URL. Kept as a first-class provider so an API-key deployment and
an OAuth deployment are the same code path with a different profile.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def api_key() -> str | None:
    for variable in ("REVIEWFLOW_OPENAI_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(variable)
        if value:
            return value
    return None


def base_url() -> str:
    return (
        os.environ.get("REVIEWFLOW_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or DEFAULT_OPENAI_BASE_URL
    )


def openai_client_kwargs() -> dict[str, Any] | None:
    """kwargs for ``AsyncOpenAI`` using an API key, or ``None`` when unset."""
    key = api_key()
    if not key:
        return None
    kwargs: dict[str, Any] = {"api_key": key, "base_url": base_url()}
    organization = os.environ.get("OPENAI_ORG_ID") or os.environ.get("OPENAI_ORGANIZATION")
    if organization:
        kwargs["organization"] = organization
    project = os.environ.get("OPENAI_PROJECT_ID")
    if project:
        kwargs["project"] = project
    timeout = os.environ.get("REVIEWFLOW_HTTP_TIMEOUT")
    if timeout:
        kwargs["timeout"] = float(timeout)
    return kwargs


def openai_status() -> dict[str, Any]:
    key = api_key()
    if not key:
        return {"available": False, "source": None}
    return {
        "available": True,
        "source": "env:REVIEWFLOW_OPENAI_API_KEY" if os.environ.get("REVIEWFLOW_OPENAI_API_KEY") else "env:OPENAI_API_KEY",
        "base_url": base_url(),
        "masked_key": f"{key[:7]}…" if len(key) > 10 else "…",
    }
