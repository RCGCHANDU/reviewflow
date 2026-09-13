"""Provider routing: one SDK ``ModelProvider`` that fronts every credentialed backend.

The Agents SDK resolves every agent's model *string* through
``RunConfig.model_provider``, which makes ``ModelProvider`` the natural
plug-and-play seam: :class:`ModelRouter` implements it and returns the right
concrete model object per provider - ``OpenAIResponsesModel`` for an endpoint that
speaks the Responses API, ``OpenAIChatCompletionsModel`` for one that speaks Chat
Completions - without any agent, prompt or stage having to know which backend it
is talking to.

Model references may name their provider explicitly with a ``provider:model``
prefix (``codex:gpt-5.6-sol``, ``commandcode:deepseek/deepseek-v4-flash``). A bare
name resolves against the active provider. Because a router is optional - the test
suite injects a scripted provider instead - every behaviour here is additive: with
no router the workflow behaves exactly as before.

Two provider-specific behaviours are properties of the *backend*, verified against
the live endpoints, not preferences:

* the Codex backend rejects non-streaming requests (``Stream must be set to true``)
  and any request with ``store=true``, so routes through it must stream and must
  carry ``ModelSettings(store=False)``;
* commandcode is a Chat Completions gateway whose WAF rejects the default urllib
  user agent.

See ``IMPLEMENTATION_NOTES.md`` for the probe transcripts behind both.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from agents import ModelSettings
from agents.models.interface import Model, ModelProvider
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI

from reviewflow.config import ModelsByRole, role_tier

#: The provider-agnostic role placeholders from :class:`Settings`. Compared against a
#: configured model name to tell "someone chose this" from "this is just the default".
GENERIC_TIER_DEFAULTS = ModelsByRole()

ApiMode = Literal["responses", "chat_completions"]

#: Providers this module knows how to build.
KNOWN_PROVIDERS: tuple[str, ...] = ("codex", "commandcode", "openai")

#: Preference order for ``provider="auto"``. commandcode comes first because it is a
#: single gateway that serves every model the workflow wants over ordinary
#: non-streaming Chat Completions (verified: plain, structured output, tool loop),
#: so the default path needs no special-casing. The Codex OAuth path is fully
#: supported but requires streaming, so it is opted into rather than defaulted to.
AUTO_ORDER: tuple[str, ...] = ("commandcode", "codex", "openai")


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


@dataclass(frozen=True)
class RoleModels:
    """Default models per role tier for one provider."""

    orchestrator: str
    reviewer: str
    editor: str
    adversarial: str
    cheap: str
    verifier: str

    def for_role(self, role_key: str, *, write_capable: bool = False, adversarial: bool = False) -> str:
        """Mirror of ``ModelsByRole.for_role`` so providers stay interchangeable.

        Shares :func:`reviewflow.config.role_tier`, so a role added to the registry
        resolves to the same tier on every provider.
        """
        tier = role_tier(role_key, write_capable=write_capable, adversarial=adversarial)
        return str(getattr(self, tier))


@dataclass(frozen=True)
class ProviderProfile:
    """Everything known about one credentialed backend."""

    name: str
    api_mode: ApiMode
    base_url: str
    models: RoleModels
    #: The backend rejects non-streaming requests, so the engine must stream.
    requires_streaming: bool = False
    #: Some backends reject ``store=true``; ``None`` leaves the SDK default alone.
    store: bool | None = None
    #: Extra identity headers beyond auth (e.g. Codex's ``originator``).
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    #: Returns ``AsyncOpenAI`` kwargs, or ``None`` when the credential is missing.
    client_kwargs: Callable[[], dict[str, Any] | None] = lambda: None

    def available(self) -> bool:
        return self.client_kwargs() is not None

    def build_client(self) -> AsyncOpenAI:
        kwargs = self.client_kwargs()
        if kwargs is None:
            raise RuntimeError(f"provider {self.name!r} has no usable credential")
        return AsyncOpenAI(**kwargs)


# --------------------------------------------------------------------------- #
# Built-in profiles
# --------------------------------------------------------------------------- #

#: Models verified usable on commandcode (plain + structured output + tool loop).
#: ``gpt-5.4-mini`` is deliberately NOT a default: on this account it returns
#: ``403 MODEL_NOT_IN_PLAN: GPT-5.4 Mini available in Pro and above plans``, so
#: defaulting to it would break every role on a non-Pro plan. ``gpt-5.6-sol`` is the
#: strong tier, ``gpt-5.6-luna`` the mid tier, ``deepseek/deepseek-v4-flash`` the
#: cheap mechanical tier.
COMMANDCODE_MODELS = RoleModels(
    orchestrator="gpt-5.6-sol",
    reviewer="gpt-5.6-luna",
    editor="gpt-5.6-sol",
    adversarial="gpt-5.6-sol",
    cheap="deepseek/deepseek-v4-flash",
    verifier="gpt-5.6-luna",
)

#: Models the ChatGPT-account Codex backend actually exposes (probed from
#: ``GET /models``): gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5.
#: ``gpt-5.4`` is rejected with "not supported when using Codex with a ChatGPT account".
CODEX_MODELS = RoleModels(
    orchestrator="gpt-5.6-sol",
    reviewer="gpt-5.6-luna",
    editor="gpt-5.6-sol",
    adversarial="gpt-5.6-sol",
    cheap="gpt-5.6-luna",
    verifier="gpt-5.6-luna",
)

OPENAI_MODELS = RoleModels(
    orchestrator="gpt-5.6-sol",
    reviewer="gpt-5.4-mini",
    editor="gpt-5.6-sol",
    adversarial="gpt-5.6-sol",
    cheap="gpt-5.4-mini",
    verifier="gpt-5.4-mini",
)


def _codex_client_kwargs() -> dict[str, Any] | None:
    from reviewflow.providers.codex_oauth import codex_client_kwargs

    return codex_client_kwargs()


def _commandcode_client_kwargs() -> dict[str, Any] | None:
    from reviewflow.providers.commandcode import commandcode_client_kwargs

    return commandcode_client_kwargs()


def _openai_client_kwargs() -> dict[str, Any] | None:
    from reviewflow.providers.openai_key import openai_client_kwargs

    return openai_client_kwargs()


def codex_profile() -> ProviderProfile:
    from reviewflow.providers.codex_oauth import CODEX_BASE_URL

    return ProviderProfile(
        name="codex",
        api_mode="responses",
        base_url=CODEX_BASE_URL,
        models=CODEX_MODELS,
        # Verified: the backend answers a non-streaming call with
        # {"detail": "Stream must be set to true"}.
        requires_streaming=True,
        # Verified: it answers store=true with {"detail": "Store must be set to false"}.
        store=False,
        extra_headers={"originator": "codex_cli_rs"},
        client_kwargs=_codex_client_kwargs,
    )


def commandcode_profile() -> ProviderProfile:
    from reviewflow.providers.commandcode import COMMANDCODE_BASE_URL

    return ProviderProfile(
        name="commandcode",
        api_mode="chat_completions",
        base_url=COMMANDCODE_BASE_URL,
        models=COMMANDCODE_MODELS,
        client_kwargs=_commandcode_client_kwargs,
    )


def openai_profile() -> ProviderProfile:
    base_url = _env("OPENAI_BASE_URL") or _env("OPENAI_API_BASE") or "https://api.openai.com/v1"
    return ProviderProfile(
        name="openai",
        api_mode="responses",
        base_url=base_url,
        models=OPENAI_MODELS,
        client_kwargs=_openai_client_kwargs,
    )


def all_profiles() -> dict[str, ProviderProfile]:
    """Every profile this build knows, credential or not."""
    return {
        "codex": codex_profile(),
        "commandcode": commandcode_profile(),
        "openai": openai_profile(),
    }


# --------------------------------------------------------------------------- #
# Model reference parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRef:
    """A model reference split into provider and bare model name."""

    provider: str
    model: str
    explicit: bool

    def canonical(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_model_ref(value: str, *, default_provider: str, known: tuple[str, ...] = KNOWN_PROVIDERS) -> ModelRef:
    """Split ``provider:model``, treating a bare name as belonging to ``default_provider``.

    A colon that is not a known provider prefix is left alone: model ids such as
    ``moonshotai/Kimi-K3`` and vendor paths must survive untouched.
    """
    head, separator, tail = value.partition(":")
    if separator and tail and head in known:
        return ModelRef(provider=head, model=tail, explicit=True)
    return ModelRef(provider=default_provider, model=value, explicit=False)


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #


class ModelRouter(ModelProvider):
    """A ``ModelProvider`` spanning every credentialed backend.

    One instance per workflow run: the underlying ``AsyncOpenAI`` clients are
    created lazily and reused, so a run does not open a client per agent call.
    """

    def __init__(
        self,
        profiles: Mapping[str, ProviderProfile],
        *,
        default_provider: str,
        model_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if default_provider not in profiles:
            raise ValueError(f"default provider {default_provider!r} is not available ({sorted(profiles)})")
        self._profiles = dict(profiles)
        self.default_provider = default_provider
        self.model_overrides = dict(model_overrides or {})
        self._clients: dict[str, AsyncOpenAI] = {}
        self._models: dict[tuple[str, str], Model] = {}

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_settings(cls, settings: Any) -> ModelRouter | None:
        """Build a router for the providers actually credentialed on this machine.

        Returns ``None`` when nothing usable is available, so the caller can fall back to
        the SDK's own default provider - which is how an API-key-only or scripted setup
        keeps working without this module having an opinion about it.
        """
        requested = (getattr(settings, "provider", "auto") or "auto").strip().lower()
        profiles = all_profiles()
        available = {name: profile for name, profile in profiles.items() if profile.available()}

        if requested == "auto":
            for name in AUTO_ORDER:
                if name in available:
                    return cls(available, default_provider=name, model_overrides=settings.model_overrides)
            return None

        if requested not in profiles:
            raise ValueError(f"unknown provider {requested!r}; expected one of auto, {', '.join(KNOWN_PROVIDERS)}")
        if requested not in available:
            return None
        # A specifically requested provider becomes the default, but any other
        # credentialed provider stays reachable through an explicit ``provider:model``.
        return cls(available, default_provider=requested, model_overrides=settings.model_overrides)

    # -- resolution --------------------------------------------------------- #

    def resolve(self, model_name: str | None) -> tuple[ProviderProfile, str]:
        """Resolve a model reference to a profile and a bare model name."""
        if not model_name:
            profile = self._profiles[self.default_provider]
            return profile, profile.models.reviewer
        ref = parse_model_ref(model_name, default_provider=self.default_provider)
        if ref.provider not in self._profiles:
            raise ValueError(
                f"{ref.canonical()!r} names provider {ref.provider!r}, which has no credential "
                f"(available: {', '.join(sorted(self._profiles))})"
            )
        return self._profiles[ref.provider], ref.model

    def model_for_role(
        self,
        role_key: str,
        *,
        write_capable: bool = False,
        adversarial: bool = False,
        configured: str | None = None,
    ) -> str:
        """Role model string, honouring explicit configuration over provider defaults.

        Precedence, most-specific first:

        1. a recorded ``model_overrides`` entry (an env var or an explicit choice);
        2. a ``configured`` value that differs from the provider-agnostic placeholder in
           :class:`ModelsByRole` - i.e. someone deliberately named this model;
        3. the active provider profile's own default for the role's tier.

        Step 2 exists because ``ModelsByRole``'s defaults are placeholders chosen without
        a provider in mind; letting them win over a profile default would route every run
        to a model named for a different backend. Step 1 is looked up by *tier as well as
        by role key*, because an env var like ``REVIEWFLOW_REVIEWER_MODEL`` is a tier and
        must still apply to ``evidence_auditor``.
        """
        tier = role_tier(role_key, write_capable=write_capable, adversarial=adversarial)
        override = self.model_overrides.get(tier) or self.model_overrides.get(role_key)
        if override:
            return override
        if configured and configured != str(getattr(GENERIC_TIER_DEFAULTS, tier)):
            return configured
        profile = self._profiles[self.default_provider]
        return profile.models.for_role(role_key, write_capable=write_capable, adversarial=adversarial)

    def requires_streaming_for(self, model_name: str | None) -> bool:
        return self.resolve(model_name)[0].requires_streaming

    def model_settings_for(self, model_name: str | None, base: ModelSettings | None = None) -> ModelSettings:
        """Apply provider-mandated model settings (currently ``store=false``).

        ``ModelSettings`` is a dataclass in this SDK version, so ``dataclasses.replace``
        is the way to derive a variant - there is no ``.copy(update=...)``.
        """
        profile, _ = self.resolve(model_name)
        settings = base if base is not None else ModelSettings()
        if profile.store is None or getattr(settings, "store", None) is not None:
            return settings
        return replace(settings, store=profile.store)

    # -- ModelProvider ------------------------------------------------------ #

    def get_model(self, model_name: str | None) -> Model:
        profile, bare = self.resolve(model_name)
        key = (profile.name, bare)
        cached = self._models.get(key)
        if cached is not None:
            return cached
        client = self._client(profile)
        model: Model
        if profile.api_mode == "responses":
            model = OpenAIResponsesModel(model=bare, openai_client=client)
        else:
            model = OpenAIChatCompletionsModel(model=bare, openai_client=client)
        self._models[key] = model
        return model

    def _client(self, profile: ProviderProfile) -> AsyncOpenAI:
        client = self._clients.get(profile.name)
        if client is None:
            client = profile.build_client()
            self._clients[profile.name] = client
        return client

    def openai_provider(self) -> OpenAIProvider:
        """An ``OpenAIProvider`` over the default client.

        Used by nested runs that need the plain SDK provider shape rather than this
        router (kept small deliberately: nested runs should resolve the same way).
        """
        return OpenAIProvider(openai_client=self._client(self._profiles[self.default_provider]))

    # -- introspection ------------------------------------------------------ #

    @property
    def profiles(self) -> Mapping[str, ProviderProfile]:
        """The credentialed providers this router can resolve."""
        return dict(self._profiles)

    def describe(self) -> dict[str, Any]:
        return {
            "default_provider": self.default_provider,
            "available": sorted(self._profiles),
            "routes": {
                name: {
                    "api_mode": profile.api_mode,
                    "base_url": profile.base_url,
                    "requires_streaming": profile.requires_streaming,
                    "store": profile.store,
                    "models": {
                        "orchestrator": profile.models.orchestrator,
                        "reviewer": profile.models.reviewer,
                        "editor": profile.models.editor,
                        "adversarial": profile.models.adversarial,
                        "cheap": profile.models.cheap,
                        "verifier": profile.models.verifier,
                    },
                }
                for name, profile in sorted(self._profiles.items())
            },
        }


def credential_status() -> dict[str, Any]:
    """Masked discovery report for the CLI: which providers can be used, and why."""
    from reviewflow.providers.codex_oauth import codex_status
    from reviewflow.providers.commandcode import commandcode_status

    return {
        "codex": {**codex_status(), "api_mode": "responses", "requires_streaming": True},
        "commandcode": {**commandcode_status(), "api_mode": "chat_completions", "requires_streaming": False},
        "openai": {
            "available": _openai_client_kwargs() is not None,
            "source": "env:OPENAI_API_KEY" if _env("OPENAI_API_KEY") else None,
            "api_mode": "responses",
            "requires_streaming": False,
        },
    }
