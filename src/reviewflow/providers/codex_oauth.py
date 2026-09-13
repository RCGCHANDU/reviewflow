"""ChatGPT OAuth (Codex) credential handling.

The Codex backend at ``https://chatgpt.com/backend-api/codex`` is a Responses API
endpoint authenticated with a ChatGPT OAuth access token rather than an API key.
Two facts drive this module, both verified against the live endpoint:

* the backend **requires** ``stream: true`` - a non-streaming request is rejected
  with ``{"detail": "Stream must be set to true"}`` - so anything routing through
  it must use the SDK's streaming path;
* third-party harnesses must identify themselves: ``originator`` plus a
  ``ChatGPT-Account-ID`` header taken from the access token's
  ``https://api.openai.com/auth.chatgpt_account_id`` claim. Without the account
  header the request is rejected as unauthorised.

Credential discovery order (first hit wins), so the user pastes nothing:

1. ``REVIEWFLOW_CODEX_ACCESS_TOKEN`` / ``OPENAI_CODEX_ACCESS_TOKEN`` in the env
2. the Hermes credential store (``$HERMES_HOME/auth.json``), which refreshes itself
3. the Codex CLI store (``~/.codex/auth.json``), which this module can refresh

Refresh uses the OAuth refresh token against ``https://auth.openai.com/oauth/token``
with the Codex CLI's public client id. A refreshed token is written back to the
store it came from, atomically, with a ``.bak`` copy kept.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Verified endpoints and the public Codex CLI client id used for refresh.
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
#: Refresh this many seconds before expiry.
REFRESH_SKEW_SECONDS = 120
USER_AGENT = "reviewflow/0.1.0"
#: The claim path that carries the ChatGPT account id.
ACCOUNT_CLAIM = "https://api.openai.com/auth"


def hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    return Path.home() / "AppData" / "Local" / "hermes" if os.name == "nt" else Path.home() / ".hermes"


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (used only for hints: exp, account id)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def token_expiry(token: str) -> int | None:
    exp = jwt_claims(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def account_id_from_token(token: str) -> str | None:
    claim = jwt_claims(token).get(ACCOUNT_CLAIM)
    if isinstance(claim, dict):
        value = claim.get("chatgpt_account_id")
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class CodexCredentials:
    """A usable Codex OAuth credential."""

    access_token: str
    refresh_token: str | None
    account_id: str | None
    source: str
    expires_at: int | None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at - time.time() < REFRESH_SKEW_SECONDS

    def masked(self) -> str:
        return f"{self.access_token[:8]}… {f'(exp in {int(self.expires_at - time.time())}s)' if self.expires_at else ''}"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _from_env() -> CodexCredentials | None:
    token = os.environ.get("REVIEWFLOW_CODEX_ACCESS_TOKEN") or os.environ.get("OPENAI_CODEX_ACCESS_TOKEN")
    if not token:
        return None
    return CodexCredentials(
        access_token=token,
        refresh_token=os.environ.get("REVIEWFLOW_CODEX_REFRESH_TOKEN"),
        account_id=account_id_from_token(token),
        source="environment",
        expires_at=token_expiry(token),
    )


def _from_hermes_store() -> CodexCredentials | None:
    """Hermes keeps its own OAuth credential fresh, so prefer it when present."""
    path = hermes_home() / "auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = ((data.get("providers") or {}).get("openai-codex") or {})
    tokens = entry.get("tokens") or {}
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    return CodexCredentials(
        access_token=access,
        refresh_token=tokens.get("refresh_token"),
        account_id=account_id_from_token(access),
        source=str(path),
        expires_at=token_expiry(access),
    )


def _codex_store_path() -> Path:
    """Location of the Codex CLI credential store.

    Overridable so a test (or a second machine profile) can point at its own store
    instead of the real one - which matters because discovery may otherwise attempt a
    token refresh, i.e. a network call, simply for existing.
    """
    override = os.environ.get("REVIEWFLOW_CODEX_AUTH_PATH")
    return Path(override) if override else Path.home() / ".codex" / "auth.json"


def _from_codex_cli_store() -> CodexCredentials | None:
    path = _codex_store_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    return CodexCredentials(
        access_token=access,
        refresh_token=tokens.get("refresh_token"),
        account_id=account_id_from_token(access) or tokens.get("account_id"),
        source=str(path),
        expires_at=token_expiry(access),
    )


def discover_codex_credentials(*, refresh: bool = True) -> CodexCredentials | None:
    """Find a Codex credential, refreshing it when it is close to expiry."""
    for finder in (_from_env, _from_hermes_store, _from_codex_cli_store):
        found = finder()
        if found is not None:
            if refresh and found.expired and found.refresh_token:
                refreshed = refresh_codex_token(found.refresh_token)
                if refreshed is not None:
                    return refreshed
            return found
    return None


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


def refresh_codex_token(refresh_token: str, *, timeout: float = 30.0) -> CodexCredentials | None:
    """Exchange a refresh token for a new access token and persist it.

    Returns ``None`` (rather than raising) when the refresh fails, so a caller can
    fall back to another provider instead of crashing the workflow.
    """
    body = json.dumps(
        {"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    request = urllib.request.Request(
        CODEX_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    new_refresh = payload.get("refresh_token") or refresh_token
    credentials = CodexCredentials(
        access_token=access,
        refresh_token=new_refresh,
        account_id=account_id_from_token(access),
        source="refreshed",
        expires_at=token_expiry(access),
    )
    _persist_refresh(access, new_refresh)
    return credentials


def _persist_refresh(access_token: str, refresh_token: str) -> None:
    """Write a refreshed token back to every store that already has one.

    Both targets are resolved through their overridable accessors, never as raw
    ``Path.home()`` concatenations: writing to a hard-coded home path would let a
    configuration or a test that redirected the store still overwrite the user's real
    login (which is exactly what happened once, and is why this comment exists).
    """
    targets: list[tuple[Path, str]] = []
    codex = _codex_store_path()
    if codex.exists():
        targets.append((codex, "codex"))
    hermes = hermes_home() / "auth.json"
    if hermes.exists():
        targets.append((hermes, "hermes"))

    for path, kind in targets:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if kind == "codex":
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            tokens["access_token"] = access_token
            tokens["refresh_token"] = refresh_token
            tokens["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        else:
            entry = (data.get("providers") or {}).get("openai-codex")
            if not isinstance(entry, dict):
                continue
            entry.setdefault("tokens", {})
            entry["tokens"]["access_token"] = access_token
            entry["tokens"]["refresh_token"] = refresh_token
            entry["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write_json(path, data)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` atomically, keeping one backup. Never leaves a partial file."""
    import tempfile

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #


def codex_headers(access_token: str, account_id: str | None = None) -> dict[str, str]:
    """Identity headers the Codex backend requires from a third-party harness."""
    headers = {
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.0.0 (reviewflow)",
        "OpenAI-Beta": "responses=experimental",
    }
    account = account_id or account_id_from_token(access_token)
    if account:
        # The backend keys entitlement off this header; without it the call is rejected.
        headers["ChatGPT-Account-ID"] = account
    return headers


def codex_client_kwargs(*, refresh: bool = True) -> dict[str, Any] | None:
    """kwargs for ``AsyncOpenAI`` that speak to the Codex backend, or ``None``.

    ``AsyncOpenAI`` requires *some* api_key value; the OAuth access token is what
    the endpoint actually authenticates on, so it is passed there.
    """
    credentials = discover_codex_credentials(refresh=refresh)
    if credentials is None:
        return None
    return {
        "base_url": CODEX_BASE_URL,
        "api_key": credentials.access_token,
        "default_headers": codex_headers(credentials.access_token, credentials.account_id),
    }


def codex_status() -> dict[str, Any]:
    """A masked status record for the CLI and logs."""
    credentials = discover_codex_credentials(refresh=False)
    if credentials is None:
        return {"available": False, "source": None}
    return {
        "available": True,
        "source": credentials.source,
        "expires_at": credentials.expires_at,
        "seconds_left": int(credentials.expires_at - time.time()) if credentials.expires_at else None,
        "refreshable": bool(credentials.refresh_token),
        "account_id": (credentials.account_id or "")[:8] + "…" if credentials.account_id else None,
        "masked_token": credentials.masked(),
    }
