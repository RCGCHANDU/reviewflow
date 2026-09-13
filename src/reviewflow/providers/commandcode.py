"""commandcode credential handling.

commandcode exposes an OpenAI-compatible Chat Completions API at
``https://api.commandcode.ai/provider/v1``. Its CLI stores a bearer key in
``~/.commandcode/auth.json`` (``{"apiKey": ...}``) after a browser login, so a
harness that reads that file is "plug and play" without the user copying a key.

Two operational facts, both verified against the live endpoint:

* the API sits behind a WAF that answers ``error code: 1010`` to the default
  ``Python-urllib`` user agent, so a real ``User-Agent`` is mandatory (the OpenAI
  client sends one);
* ``response_format`` with a strict JSON schema is honoured, so structured
  outputs work over the Chat Completions transport.

Discovery order (first hit wins):
1. ``REVIEWFLOW_COMMANDCODE_API_KEY`` / ``COMMANDCODE_API_KEY``
2. ``~/.commandcode/auth.json``
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMANDCODE_BASE_URL = "https://api.commandcode.ai/provider/v1"
USER_AGENT = "reviewflow/0.1.0"


@dataclass(frozen=True)
class CommandCodeCredential:
    api_key: str
    source: str
    user: str | None = None

    def masked(self) -> str:
        prefix = self.api_key[:9] if len(self.api_key) > 12 else ""
        return f"{prefix}…({len(self.api_key)} chars)"


def _auth_path() -> Path:
    override = os.environ.get("REVIEWFLOW_COMMANDCODE_AUTH_PATH")
    return Path(override) if override else Path.home() / ".commandcode" / "auth.json"


def discover_commandcode_credential() -> CommandCodeCredential | None:
    """Find a commandcode bearer key without asking the user for one."""
    for variable in ("REVIEWFLOW_COMMANDCODE_API_KEY", "COMMANDCODE_API_KEY"):
        value = os.environ.get(variable)
        if value:
            return CommandCodeCredential(api_key=value, source=f"env:{variable}")
    path = _auth_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("apiKey") or data.get("api_key")
    if not isinstance(key, str) or not key:
        return None
    return CommandCodeCredential(api_key=key, source=str(path), user=data.get("userName") or data.get("userId"))


def commandcode_client_kwargs() -> dict[str, Any] | None:
    """kwargs for ``AsyncOpenAI`` pointed at commandcode, or ``None``."""
    credential = discover_commandcode_credential()
    if credential is None:
        return None
    return {
        "base_url": COMMANDCODE_BASE_URL,
        "api_key": credential.api_key,
        "default_headers": {"User-Agent": USER_AGENT},
        "timeout": float(os.environ.get("REVIEWFLOW_HTTP_TIMEOUT", "600")),
    }


def commandcode_status() -> dict[str, Any]:
    credential = discover_commandcode_credential()
    if credential is None:
        return {"available": False, "source": None}
    return {
        "available": True,
        "source": credential.source,
        "user": credential.user,
        "masked_key": credential.masked(),
    }
