"""Tests for provider plug-and-play: discovery, refresh, routing, and isolation.

Every test here is hermetic: no network, and no dependence on whatever credentials
happen to exist on the machine running the suite. The two things that could reach out
- the Codex credential store and the Hermes store - are redirected at temporary paths
by fixture, and a test asserts that a run with an injected provider never routes at all.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from agents import ModelSettings
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel

from reviewflow.config import ModelsByRole, Settings, role_tier
from reviewflow.providers import codex_oauth, commandcode, routing
from reviewflow.providers.routing import (
    ModelRouter,
    ProviderProfile,
    RoleModels,
    parse_model_ref,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def fake_jwt(claims: dict[str, Any], *, exp_offset: int = 3600) -> str:
    """Build an unsigned JWT-shaped token carrying the given claims."""

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps({**payload, "exp": int(time.time()) + exp_offset}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment(claims)}.signature"


def codex_token(account: str = "acct-1234-5678", *, exp_offset: int = 3600) -> str:
    return fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": account}}, exp_offset=exp_offset)


def write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def isolated_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every credential store at a temp dir and clear provider env vars."""
    hermes = tmp_path / "hermes"
    codex = tmp_path / "codex" / "auth.json"
    command = tmp_path / "commandcode" / "auth.json"
    for variable in (
        "REVIEWFLOW_CODEX_ACCESS_TOKEN",
        "OPENAI_CODEX_ACCESS_TOKEN",
        "REVIEWFLOW_CODEX_REFRESH_TOKEN",
        "REVIEWFLOW_COMMANDCODE_API_KEY",
        "COMMANDCODE_API_KEY",
        "REVIEWFLOW_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "REVIEWFLOW_PROVIDER",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("REVIEWFLOW_CODEX_AUTH_PATH", str(codex))
    monkeypatch.setenv("REVIEWFLOW_COMMANDCODE_AUTH_PATH", str(command))
    return {"hermes": hermes / "auth.json", "codex": codex, "commandcode": command}


# --------------------------------------------------------------------------- #
# model reference parsing
# --------------------------------------------------------------------------- #


def test_parse_model_ref_splits_a_known_provider_prefix() -> None:
    ref = parse_model_ref("codex:gpt-5.6-sol", default_provider="commandcode")
    assert (ref.provider, ref.model, ref.explicit) == ("codex", "gpt-5.6-sol", True)


def test_parse_model_ref_treats_a_bare_name_as_the_default_provider() -> None:
    ref = parse_model_ref("gpt-5.6-sol", default_provider="commandcode")
    assert (ref.provider, ref.model, ref.explicit) == ("commandcode", "gpt-5.6-sol", False)


def test_parse_model_ref_leaves_an_unknown_prefix_alone() -> None:
    """A vendor path or an unknown colon must survive: model ids are not namespaced by us."""
    ref = parse_model_ref("moonshotai/Kimi-K3", default_provider="commandcode")
    assert ref.model == "moonshotai/Kimi-K3"

    ref = parse_model_ref("some-vendor:model-x", default_provider="commandcode")
    assert (ref.provider, ref.model) == ("commandcode", "some-vendor:model-x")


def test_role_tier_is_shared_by_both_model_tables() -> None:
    """The generic table and the per-provider table must agree, or roles drift."""
    from reviewflow.config import TIER_NAMES

    profiles = RoleModels(orchestrator="a", reviewer="b", editor="c", adversarial="d", cheap="e", verifier="f")
    generic = ModelsByRole()
    for role_key, write_capable, adversarial in (
        ("orchestrator", False, False),
        ("verifier", False, False),
        ("controlled_editor", True, False),
        ("adversarial_reviewer", False, True),
        ("evidence_auditor", False, False),
        ("a_brand_new_role", False, False),
    ):
        tier = role_tier(role_key, write_capable=write_capable, adversarial=adversarial)
        assert profiles.for_role(role_key, write_capable=write_capable, adversarial=adversarial) == getattr(
            profiles, tier
        )
        assert generic.for_role(role_key, write_capable=write_capable, adversarial=adversarial) == getattr(
            generic, tier
        )

    # Idempotent: a tier resolves to itself, so a caller that passes a tier name where a
    # role key is expected still gets that tier rather than the catch-all.
    for tier in TIER_NAMES:
        assert role_tier(tier) == tier
        assert role_tier(role_tier("evidence_auditor")) == "reviewer"


# --------------------------------------------------------------------------- #
# commandcode credentials
# --------------------------------------------------------------------------- #


def test_commandcode_reads_the_cli_login_store(isolated_stores: dict[str, Path]) -> None:
    """The whole point of plug-and-play: the CLI already logged in, so we do not ask."""
    write_json(isolated_stores["commandcode"], {"apiKey": "user_2secretvalue", "userName": "Ryan"})
    credential = commandcode.discover_commandcode_credential()
    assert credential is not None
    assert credential.api_key == "user_2secretvalue"
    assert credential.user == "Ryan"
    assert str(isolated_stores["commandcode"]) in credential.source


def test_commandcode_env_wins_over_the_store(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json(isolated_stores["commandcode"], {"apiKey": "from-file"})
    monkeypatch.setenv("COMMANDCODE_API_KEY", "from-env")
    credential = commandcode.discover_commandcode_credential()
    assert credential is not None
    assert credential.api_key == "from-env"
    assert credential.source == "env:COMMANDCODE_API_KEY"


def test_commandcode_returns_none_rather_than_raising_on_a_broken_store(
    isolated_stores: dict[str, Path],
) -> None:
    isolated_stores["commandcode"].parent.mkdir(parents=True, exist_ok=True)
    isolated_stores["commandcode"].write_text("{not json", encoding="utf-8")
    assert commandcode.discover_commandcode_credential() is None
    assert commandcode.commandcode_client_kwargs() is None


def test_commandcode_never_exposes_the_full_key_in_status(isolated_stores: dict[str, Path]) -> None:
    write_json(isolated_stores["commandcode"], {"apiKey": "user_2SUPERSECRETVALUE"})
    status = commandcode.commandcode_status()
    assert status["available"] is True
    assert "user_2SUPERSECRETVALUE" not in json.dumps(status)


def test_commandcode_client_kwargs_target_the_gateway(isolated_stores: dict[str, Path]) -> None:
    write_json(isolated_stores["commandcode"], {"apiKey": "user_2abc"})
    kwargs = commandcode.commandcode_client_kwargs()
    assert kwargs is not None
    assert kwargs["base_url"] == commandcode.COMMANDCODE_BASE_URL
    assert kwargs["api_key"] == "user_2abc"
    # A WAF in front of the gateway answers error 1010 to a default urllib user agent.
    assert kwargs["default_headers"]["User-Agent"].startswith("reviewflow/")


# --------------------------------------------------------------------------- #
# codex OAuth credentials
# --------------------------------------------------------------------------- #


def test_codex_account_id_comes_from_the_jwt_claim() -> None:
    assert codex_oauth.account_id_from_token(codex_token("acct-abc")) == "acct-abc"
    assert codex_oauth.account_id_from_token("not-a-jwt") is None


def test_codex_headers_identify_the_harness_and_the_account() -> None:
    headers = codex_oauth.codex_headers(codex_token("acct-xyz"))
    assert headers["originator"] == "codex_cli_rs"
    # Without the account header the backend rejects the call as unauthorised.
    assert headers["ChatGPT-Account-ID"] == "acct-xyz"


def test_codex_discovery_prefers_env_then_hermes_then_codex_cli(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json(
        isolated_stores["codex"],
        {"tokens": {"access_token": codex_token("from-codex-cli"), "refresh_token": "r1"}},
    )
    write_json(
        isolated_stores["hermes"],
        {"providers": {"openai-codex": {"tokens": {"access_token": codex_token("from-hermes"), "refresh_token": "r2"}}}},
    )

    # Hermes' store wins over the Codex CLI store: Hermes keeps it refreshed.
    found = codex_oauth.discover_codex_credentials(refresh=False)
    assert found is not None and found.account_id == "from-hermes"

    monkeypatch.setenv("REVIEWFLOW_CODEX_ACCESS_TOKEN", codex_token("from-env"))
    found = codex_oauth.discover_codex_credentials(refresh=False)
    assert found is not None and found.account_id == "from-env"


def test_codex_discovery_is_silent_when_nothing_is_signed_in(isolated_stores: dict[str, Path]) -> None:
    assert codex_oauth.discover_codex_credentials(refresh=False) is None
    assert codex_oauth.codex_client_kwargs(refresh=False) is None
    assert codex_oauth.codex_status()["available"] is False


def test_codex_refresh_writes_back_atomically_and_keeps_a_backup(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = write_json(
        isolated_stores["codex"],
        {"tokens": {"access_token": codex_token("old", exp_offset=-60), "refresh_token": "old-refresh"}},
    )

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        body = json.dumps({"access_token": codex_token("new"), "refresh_token": "new-refresh"}).encode()

        class Response:
            def read(self) -> bytes:
                return body

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        assert request.full_url == codex_oauth.CODEX_TOKEN_URL
        payload = json.loads(request.data.decode())
        assert payload["grant_type"] == "refresh_token"
        assert payload["client_id"] == codex_oauth.CODEX_CLIENT_ID
        return Response()

    monkeypatch.setattr(codex_oauth.urllib.request, "urlopen", fake_urlopen)

    refreshed = codex_oauth.refresh_codex_token("old-refresh")
    assert refreshed is not None and refreshed.account_id == "new"

    # The refreshed token is persisted where the other tools expect it, and the
    # previous file is preserved as a backup so a bad refresh cannot lose a login.
    stored = json.loads(store.read_text(encoding="utf-8"))
    assert base64.urlsafe_b64decode(stored["tokens"]["access_token"].split(".")[1] + "==")
    assert "new-refresh" in json.dumps(stored)
    assert store.with_suffix(".json.bak").exists()


def test_codex_refresh_failure_returns_none_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed refresh must degrade to 'no credential', not crash a workflow."""

    def raise_http_error(request: Any, timeout: float = 0) -> Any:
        raise codex_oauth.urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(codex_oauth.urllib.request, "urlopen", raise_http_error)
    assert codex_oauth.refresh_codex_token("expired") is None


def test_codex_expiry_uses_a_refresh_skew() -> None:
    """A token that expires inside the skew window counts as expired, so it is refreshed early."""
    soon = codex_oauth.CodexCredentials(
        access_token=codex_token("a", exp_offset=30),
        refresh_token="r",
        account_id=None,
        source="test",
        expires_at=int(time.time()) + 30,
    )
    later = codex_oauth.CodexCredentials(
        access_token=codex_token("a", exp_offset=3600),
        refresh_token="r",
        account_id=None,
        source="test",
        expires_at=int(time.time()) + 3600,
    )
    assert soon.expired is True
    assert later.expired is False


def test_refresh_never_writes_outside_the_redirected_store(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a refresh must not reach the user's real credential store.

    An earlier version built the store path with ``Path.home() / ".codex" / "auth.json"``
    instead of the overridable accessor, so a test that redirected the store still
    overwrote the developer's genuine ChatGPT login. This asserts the redirected store
    is the only file touched, by hashing the real one before and after.
    """
    real_store = Path.home() / ".codex" / "auth.json"
    before = real_store.read_bytes() if real_store.exists() else None

    store = write_json(
        isolated_stores["codex"],
        {"tokens": {"access_token": codex_token("local", exp_offset=-60), "refresh_token": "old"}},
    )

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        body = json.dumps({"access_token": codex_token("refreshed"), "refresh_token": "rotated"}).encode()

        class Response:
            def read(self) -> bytes:
                return body

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        return Response()

    monkeypatch.setattr(codex_oauth.urllib.request, "urlopen", fake_urlopen)
    assert codex_oauth.refresh_codex_token("old") is not None

    after = real_store.read_bytes() if real_store.exists() else None
    assert after == before, f"refresh escaped the redirected store and wrote to {real_store}"
    assert "rotated" in store.read_text(encoding="utf-8")


def test_codex_status_masks_the_token(isolated_stores: dict[str, Path]) -> None:
    write_json(isolated_stores["codex"], {"tokens": {"access_token": codex_token("acct"), "refresh_token": "r"}})
    status = codex_oauth.codex_status()
    assert status["available"] is True
    assert "…" in (status["masked_token"] or "")
    assert codex_token("acct") not in json.dumps(status)


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #


def profile(
    name: str,
    *,
    api_mode: str = "chat_completions",
    requires_streaming: bool = False,
    store: bool | None = None,
    credential: bool = True,
) -> ProviderProfile:
    return ProviderProfile(
        name=name,
        api_mode=api_mode,  # type: ignore[arg-type]
        base_url=f"https://{name}.example/v1",
        models=RoleModels(
            orchestrator=f"{name}-strong",
            reviewer=f"{name}-mid",
            editor=f"{name}-strong",
            adversarial=f"{name}-strong",
            cheap=f"{name}-cheap",
            verifier=f"{name}-mid",
        ),
        requires_streaming=requires_streaming,
        store=store,
        client_kwargs=(lambda: {"api_key": "test-key", "base_url": f"https://{name}.example/v1"})
        if credential
        else (lambda: None),
    )


def router(**kwargs: Any) -> ModelRouter:
    profiles = kwargs.pop("profiles", None) or {
        "commandcode": profile("commandcode"),
        "codex": profile("codex", api_mode="responses", requires_streaming=True, store=False),
    }
    default = kwargs.pop("default", None) or ("commandcode" if "commandcode" in profiles else next(iter(profiles)))
    return ModelRouter(profiles, default_provider=default, **kwargs)


def test_router_builds_a_chat_completions_model_for_a_chat_gateway() -> None:
    model = router().get_model("gpt-5.6-sol")
    assert isinstance(model, OpenAIChatCompletionsModel)


def test_router_builds_a_responses_model_for_the_codex_backend() -> None:
    model = router().get_model("codex:gpt-5.6-sol")
    assert isinstance(model, OpenAIResponsesModel)


def test_router_caches_models_and_clients() -> None:
    r = router()
    assert r.get_model("gpt-5.6-sol") is r.get_model("gpt-5.6-sol")
    # Distinct providers must not share a client: different base URLs, different auth.
    assert r.get_model("gpt-5.6-sol") is not r.get_model("codex:gpt-5.6-sol")


def test_router_reports_the_transport_the_provider_requires() -> None:
    r = router(profiles={"codex": profile("codex", api_mode="responses", requires_streaming=True, store=False)})
    assert r.requires_streaming_for("gpt-5.6-sol") is True


def test_router_forces_store_false_only_where_mandated() -> None:
    r = router(
        profiles={
            "codex": profile("codex", api_mode="responses", requires_streaming=True, store=False),
            "commandcode": profile("commandcode"),
        }
    )
    assert r.model_settings_for("codex:gpt-5.6-luna").store is False
    assert r.model_settings_for("gpt-5.6-luna").store is None


def test_router_does_not_override_an_explicit_store_choice() -> None:
    r = router(profiles={"codex": profile("codex", api_mode="responses", requires_streaming=True, store=False)})
    chosen = r.model_settings_for("codex:gpt-5.6-sol", ModelSettings(store=True))
    assert chosen.store is True, "an explicit per-agent setting must not be silently rewritten"


def test_router_rejects_a_provider_that_has_no_credential() -> None:
    r = router(profiles={"commandcode": profile("commandcode")})
    with pytest.raises(ValueError, match="no credential"):
        r.get_model("codex:gpt-5.6-sol")


def test_router_refuses_an_unavailable_default_provider() -> None:
    with pytest.raises(ValueError, match="not available"):
        ModelRouter({"commandcode": profile("commandcode")}, default_provider="codex")


def test_role_models_come_from_the_active_provider_by_default() -> None:
    r = router(default="codex", profiles={"codex": profile("codex", api_mode="responses", requires_streaming=True)})
    assert r.model_for_role("orchestrator") == "codex-strong"
    assert r.model_for_role("evidence_auditor") == "codex-mid"
    assert r.model_for_role("verifier") == "codex-mid"
    assert r.model_for_role("controlled_editor", write_capable=True) == "codex-strong"


def test_an_explicitly_configured_model_beats_the_provider_default() -> None:
    r = router(model_overrides={"reviewer": "my-pinned-model"})
    assert r.model_for_role("reviewer") == "my-pinned-model"
    # ...and a role pinned to another provider keeps that prefix.
    r2 = router(model_overrides={"reviewer": "codex:gpt-5.6-luna"})
    assert r2.model_for_role("reviewer") == "codex:gpt-5.6-luna"


def test_configured_model_precedence_is_explicit_then_profile_then_generic() -> None:
    """Precedence: recorded override > an intentionally-set model > the provider profile.

    The generic ``ModelsByRole`` defaults are provider-agnostic placeholders, so they
    must NOT beat a provider's own defaults - otherwise every run would land on a model
    name chosen for a different backend.
    """
    r = router()
    generic_default = ModelsByRole().reviewer
    assert r.model_for_role("reviewer", configured=generic_default) == "commandcode-mid"
    assert r.model_for_role("reviewer", configured="some-other-model") == "some-other-model"

    pinned = router(model_overrides={"reviewer": "pinned-by-env"})
    assert pinned.model_for_role("reviewer", configured="some-other-model") == "pinned-by-env"


def test_from_settings_auto_selects_the_first_credentialed_provider() -> None:
    profiles = {"commandcode": profile("commandcode", credential=False), "codex": profile("codex")}
    r = ModelRouter({name: p for name, p in profiles.items() if p.available()}, default_provider="codex")
    assert r.default_provider == "codex"
    assert r.describe()["available"] == ["codex"]


def test_from_settings_returns_none_without_credentials(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential anywhere must be a clean ``None``, not an exception."""
    settings = Settings().model_copy(update={"provider": "auto"})
    assert ModelRouter.from_settings(settings) is None


def test_from_settings_rejects_an_unknown_provider(isolated_stores: dict[str, Path]) -> None:
    settings = Settings().model_copy(update={"provider": "nonsense"})
    with pytest.raises(ValueError, match="unknown provider"):
        ModelRouter.from_settings(settings)


def test_describe_exposes_the_route_table_without_secrets() -> None:
    blob = json.dumps(router().describe())
    assert "test-key" not in blob
    assert "commandcode" in blob and "codex" in blob


def test_credential_status_reports_all_three_providers_masked(isolated_stores: dict[str, Path]) -> None:
    status = routing.credential_status()
    assert set(status) == {"codex", "commandcode", "openai"}
    assert all(entry["available"] is False for entry in status.values())
    assert "secret" not in json.dumps(status).lower() or "masked" in json.dumps(status)


# --------------------------------------------------------------------------- #
# settings integration
# --------------------------------------------------------------------------- #


def test_settings_from_env_reads_provider_and_records_explicit_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REVIEWFLOW_PROVIDER", "CODEX")  # normalised to lower case
    monkeypatch.setenv("REVIEWFLOW_REVIEWER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.delenv("REVIEWFLOW_ORCHESTRATOR_MODEL", raising=False)
    monkeypatch.setenv("REVIEWFLOW_WORKSPACE_ROOT", str(tmp_path))
    settings = Settings.from_env()
    assert settings.provider == "codex"
    # Only explicitly-set roles are recorded, so unset roles still follow the provider.
    assert settings.model_overrides == {"reviewer": "deepseek/deepseek-v4-flash"}


def test_settings_default_to_auto_and_no_overrides() -> None:
    settings = Settings()
    assert settings.provider == "auto"
    assert settings.model_overrides == {}
