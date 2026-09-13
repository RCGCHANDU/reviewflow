"""Probe the locally-available providers: what works, what doesn't.

Answers three questions with real requests (no tokens are ever printed):

1. Can we discover a usable credential without the user pasting anything?
2. Does the endpoint accept a NON-STREAMING request? (the workflow engine is
   non-streaming, so this is decisive for the OpenAI Codex OAuth path)
3. Does it honour ``response_format: json_schema``? The whole framework depends on
   structured outputs; without them every contract falls back to malformed-output
   retries.

Run: python tools/probe_providers.py [--provider codex|commandcode]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.request
from pathlib import Path

USER_AGENT = "reviewflow/0.1.0 (+https://commandcode.ai)"
HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or HOME / "AppData" / "Local" / "hermes")

COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"
CODEX_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

REPORT: dict[str, object] = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "checks": []}


def note(check: str, ok: bool, detail: str = "") -> None:
    REPORT["checks"].append({"check": check, "ok": ok, "detail": detail})  # type: ignore[union-attr]
    print(f"  [{'OK ' if ok else 'FAIL'}] {check}" + (f" — {detail}" if detail else ""))


def jwt_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def post(url: str, payload: dict, headers: dict[str, str], timeout: float = 60.0) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    merged = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(url, data=body, headers=merged, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def get(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[int, str]:
    merged = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, headers=merged, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def discover_commandcode_key() -> str | None:
    env = os.environ.get("COMMANDCODE_API_KEY")
    if env:
        return env
    path = HOME / ".commandcode" / "auth.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            key = data.get("apiKey")
            if isinstance(key, str) and key:
                return key
        except Exception:
            return None
    return None


def discover_codex_tokens() -> tuple[dict | None, str]:
    """Prefer Hermes' store (it refreshes), then the Codex CLI store."""
    hermes = HERMES_HOME / "auth.json"
    if hermes.exists():
        try:
            data = json.loads(hermes.read_text(encoding="utf-8"))
            tokens = (data.get("providers") or {}).get("openai-codex", {}).get("tokens")
            if isinstance(tokens, dict) and tokens.get("access_token"):
                return tokens, f"hermes auth.json ({hermes})"
        except Exception:
            pass
    codex = HOME / ".codex" / "auth.json"
    if codex.exists():
        try:
            data = json.loads(codex.read_text(encoding="utf-8"))
            tokens = data.get("tokens")
            if isinstance(tokens, dict) and tokens.get("access_token"):
                return tokens, f"codex auth.json ({codex})"
        except Exception:
            pass
    return None, "not found"


def refresh_codex(refresh_token: str) -> dict | None:
    status, body = post(
        CODEX_TOKEN_URL,
        {"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token},
        {"User-Agent": "reviewflow/0.1.0"},
    )
    print(f"       refresh -> HTTP {status}")
    if status != 200:
        print(f"       body: {body[:300]}")
        return None
    return json.loads(body)


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "Probe",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string", "enum": ["one", "two"]}, "count": {"type": "integer"}},
            "required": ["verdict", "count"],
            "additionalProperties": False,
        },
    },
}


def probe_commandcode() -> None:
    print("\n=== commandcode (chat_completions) ===")
    key = discover_commandcode_key()
    note("credential discovered", bool(key), "env COMMANDCODE_API_KEY" if os.environ.get("COMMANDCODE_API_KEY") else "~/.commandcode/auth.json")
    if not key:
        return

    status, body = get(f"{COMMANDCODE_BASE}/models")
    models: list[str] = []
    if status == 200:
        try:
            models = [m["id"] for m in json.loads(body).get("data", []) if isinstance(m, dict) and "id" in m]
        except Exception:
            models = []
    note("GET /models", status == 200, f"{len(models)} models" + (f", sample={models[:4]}" if models else ""))
    REPORT["commandcode_models"] = models

    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Reply with the JSON object only."}],
        "response_format": JSON_SCHEMA,
        "max_tokens": 200,
    }
    status, body = post(f"{COMMANDCODE_BASE}/chat/completions", payload, headers)
    ok = status == 200
    detail = ""
    if ok:
        try:
            content = json.loads(body)["choices"][0]["message"]["content"]
            detail = f"content={content[:120]!r}"
            REPORT["commandcode_json_schema"] = True
        except Exception as exc:
            ok, detail = False, f"unexpected shape: {exc}"
    else:
        detail = body[:220]
    note("chat/completions non-streaming + json_schema", ok, detail)

    if not ok:
        payload.pop("response_format", None)
        status, body = post(f"{COMMANDCODE_BASE}/chat/completions", payload, headers)
        note("chat/completions plain (no response_format)", status == 200, body[:200] if status != 200 else "plain call works")


def probe_codex() -> None:
    print("\n=== openai-codex (Responses, ChatGPT OAuth) ===")
    tokens, source = discover_codex_tokens()
    note("credential discovered", tokens is not None, source)
    if not tokens:
        return
    access = tokens["access_token"]
    claims = jwt_claims(access)
    account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or tokens.get("account_id")
    expires_at = claims.get("exp")
    remaining = int(expires_at - time.time()) if isinstance(expires_at, (int, float)) else None
    note("access token claims", bool(claims), f"exp in {remaining}s" if remaining is not None else "no exp claim")

    if remaining is not None and remaining < 120:
        fresh = refresh_codex(tokens["refresh_token"])
        if fresh:
            access = fresh.get("access_token", access)
            tokens = {**tokens, **fresh}
            claims = jwt_claims(access)
            account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or account_id
            note("token refresh", True, f"new exp in {int(claims.get('exp', 0) - time.time())}s")

    headers = {
        "Authorization": f"Bearer {access}",
        "User-Agent": "codex_cli_rs/0.0.0 (reviewflow)",
        "originator": "codex_cli_rs",
        "OpenAI-Beta": "responses=experimental",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = str(account_id)

    status, body = get(f"{CODEX_BASE}/models?client_version=1.0.0", headers)
    model_ids: list[str] = []
    if status == 200:
        try:
            raw = json.loads(body)
            items = raw.get("models") or raw.get("data") or []
            model_ids = [m.get("slug") or m.get("id") for m in items if isinstance(m, dict)]
        except Exception:
            model_ids = []
    note("GET /models", status == 200, f"{len(model_ids)} models: {model_ids[:8]}")
    REPORT["codex_models"] = model_ids

    model = os.environ.get("PROBE_CODEX_MODEL", "gpt-5.6-sol")
    payload = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with the JSON object only."}]}],
        "text": {"format": {"type": "json_schema", "name": "Probe", "strict": True, "schema": JSON_SCHEMA["json_schema"]["schema"]}},
        "stream": False,
        "store": False,
    }
    status, body = post(f"{CODEX_BASE}/responses", payload, headers, timeout=90.0)
    ok = status == 200
    detail = ""
    if ok:
        try:
            data = json.loads(body)
            text = ""
            for item in data.get("output", []):
                for chunk in item.get("content", []) or []:
                    text += chunk.get("text", "")
            detail = f"text={text[:140]!r}"
            REPORT["codex_json_schema"] = True
            REPORT["codex_nonstreaming"] = True
        except Exception as exc:
            ok, detail = False, f"unexpected shape: {exc}"
    else:
        detail = body[:260]
    note(f"POST /responses non-streaming json_schema ({model})", ok, detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["codex", "commandcode", "both"], default="both")
    args = parser.parse_args()
    print(f"reviewflow provider probe — {REPORT['when']}")
    if args.provider in ("commandcode", "both"):
        probe_commandcode()
    if args.provider in ("codex", "both"):
        probe_codex()
    out = Path("artifacts/provider_probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, indent=2), encoding="utf-8")
    print(f"\nreport: {out}")
    failed = [c for c in REPORT["checks"] if not c["ok"]]  # type: ignore[union-attr]
    print(f"{len(REPORT['checks']) - len(failed)}/{len(REPORT['checks'])} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
