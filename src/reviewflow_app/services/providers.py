"""Provider and model services: what the UI is allowed to know, and nothing more.

The security rule for this module is absolute: it returns *metadata about* credentials,
never credentials. The engine's status helpers already return masked forms; this module
re-exposes them under the API's shape and adds the derived information the UI needs
(which models exist, which roles map to which tier, whether a run would even start).

Model names are never hard-coded here either - they come from the router's profiles (which
were verified against the live endpoints) or from a live catalogue fetch. The UI renders
whatever this module reports.
"""

from __future__ import annotations

import json
import urllib.request

from reviewflow.agents.factory import AgentFactory
from reviewflow.agents.registry import build_default_registry
from reviewflow.config import Settings, role_tier
from reviewflow.observability import get_logger
from reviewflow.providers.doctor import run_doctor
from reviewflow.providers.routing import KNOWN_PROVIDERS, ModelRouter, credential_status
from reviewflow_app.config import AppSettings
from reviewflow_app.errors import ProviderUnavailableError
from reviewflow_app.schemas import (
    DoctorCheck,
    DoctorResponse,
    ModelInfo,
    PreflightCheck,
    PreflightRequest,
    PreflightResult,
    ProviderInfo,
    ProvidersResponse,
    RoleInfo,
)

_log = get_logger("app.providers")

#: Hints shown when a provider cannot be used. Written for a human who has to fix it.
PROVIDER_HINTS: dict[str, str] = {
    "codex": (
        "Sign in with the Codex CLI (`codex login`) or add the provider in Hermes "
        "(`hermes auth add openai-codex`). Tokens are refreshed automatically."
    ),
    "commandcode": "Sign in with the commandcode CLI so ~/.commandcode/auth.json exists.",
    "openai": "Set OPENAI_API_KEY (or REVIEWFLOW_OPENAI_API_KEY) for the API-key path.",
}

#: Remote catalogue endpoints, mirroring how the providers are actually probed.
CATALOGUE_URLS: dict[str, str] = {
    "commandcode": "https://api.commandcode.ai/provider/v1/models",
    "codex": "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
}


class ProviderService:
    """Read-only view of provider availability, models and role routing."""

    def __init__(self, app_settings: AppSettings) -> None:
        self.app_settings = app_settings

    # -- discovery ------------------------------------------------------------ #

    def _settings(self, provider: str | None = None) -> Settings:
        """Engine settings for a provider, without touching a credential store."""
        base = Settings.from_env()
        return base.model_copy(update={"provider": provider}) if provider else base

    def _router(self, provider: str | None = None) -> ModelRouter | None:
        return ModelRouter.from_settings(self._settings(provider))

    def status(self, *, provider: str | None = None) -> ProvidersResponse:
        """Masked provider status plus the routing table the UI renders."""
        requested = provider or Settings.from_env().provider
        credentials = credential_status()
        router = self._router(provider)

        providers: list[ProviderInfo] = []
        for name in KNOWN_PROVIDERS:
            entry = credentials.get(name, {})
            available = bool(entry.get("available"))
            api_mode = str(entry.get("api_mode", "responses"))
            # Never forward a token or key prefix to a browser. The account identifier is
            # either a user name or a truncated account id - identifying, not usable.
            if name == "commandcode":
                account = entry.get("user")
            elif name == "codex":
                account = entry.get("account_id")
            else:
                account = None
            providers.append(
                ProviderInfo(
                    name=name,
                    available=available,
                    api_mode=api_mode,
                    requires_streaming=bool(entry.get("requires_streaming", False)),
                    auth_source=entry.get("source"),
                    account=account,
                    expires_at=entry.get("expires_at"),
                    seconds_left=entry.get("seconds_left"),
                    refreshable=bool(entry.get("refreshable", False)),
                    base_url=entry.get("base_url"),
                    hint="" if available else PROVIDER_HINTS.get(name, ""),
                )
            )

        routing = router.describe() if router else {}
        role_defaults: dict[str, str] = {}
        if router:
            for tier in ("orchestrator", "reviewer", "editor", "adversarial", "cheap", "verifier"):
                role_defaults[tier] = router.model_for_role(tier)

        return ProvidersResponse(
            active_provider=router.default_provider if router else None,
            requested_provider=requested,
            providers=providers,
            routing=routing,
            role_defaults=role_defaults,
        )

    def refresh(self) -> ProvidersResponse:
        """Re-run discovery (credentials are read on every call, so this is a re-report)."""
        return self.status()

    def require_available(self, provider: str | None) -> ModelRouter:
        """Return a router or explain precisely why a run cannot start."""
        router = self._router(provider)
        if router is None:
            requested = provider or "auto"
            raise ProviderUnavailableError(
                f"No usable model provider credential was found for provider '{requested}'.",
                detail={
                    "provider": requested,
                    "hint": "Run a provider refresh on the Providers page, or sign in to a provider CLI.",
                    "checked": list(KNOWN_PROVIDERS),
                },
            )
        return router

    # -- models --------------------------------------------------------------- #

    def models(self, *, provider: str | None = None, catalogue: bool = False) -> list[ModelInfo]:
        """Models available for selection.

        Role defaults come from the router's profiles. ``catalogue=True`` additionally asks
        each provider for its full model list over the network; that call is separated out
        because it is slow, can fail, and must never be on the path that renders a page.
        """
        router = self._router(provider)
        if router is None:
            return []
        models: dict[tuple[str, str], ModelInfo] = {}
        for name, profile in router.profiles.items():
            defaults = {
                "orchestrator": profile.models.orchestrator,
                "reviewer": profile.models.reviewer,
                "editor": profile.models.editor,
                "adversarial": profile.models.adversarial,
                "cheap": profile.models.cheap,
                "verifier": profile.models.verifier,
            }
            for tier, model_id in defaults.items():
                key = (name, model_id)
                entry = models.get(key) or ModelInfo(
                    id=model_id,
                    provider=name,
                    label=f"{model_id} · {name}",
                    default_for=[],
                )
                if tier not in entry.default_for:
                    entry.default_for.append(tier)
                models[key] = entry
            if catalogue:
                for model_id in self.fetch_catalogue(name):
                    key = (name, model_id)
                    if key not in models:
                        models[key] = ModelInfo(id=model_id, provider=name, label=f"{model_id} · {name}")
        return sorted(models.values(), key=lambda item: (item.provider, item.id))

    def fetch_catalogue(self, provider: str) -> list[str]:
        """Ask a provider for its model list. Returns ids only, never a credential.

        Failures are swallowed to an empty list on purpose: a catalogue is a convenience,
        and a provider that is down should degrade to "the models we already know" rather
        than break the settings page.
        """
        url = CATALOGUE_URLS.get(provider)
        if not url:
            return []
        headers = {"Accept": "application/json", "User-Agent": "reviewflow-app/0.1.0"}
        from reviewflow.providers.codex_oauth import codex_client_kwargs

        if provider == "commandcode":
            from reviewflow.providers.commandcode import discover_commandcode_credential

            credential = discover_commandcode_credential()
            if credential is None:
                return []
            headers["Authorization"] = f"Bearer {credential.api_key}"
        elif provider == "codex":
            kwargs = codex_client_kwargs()
            if not kwargs:
                return []
            headers["Authorization"] = f"Bearer {kwargs['api_key']}"
            headers.update(kwargs["default_headers"])
        else:
            return []
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:
            _log.warning("model catalogue fetch failed", extra={"role": "app", "provider": provider})
            return []
        items = body.get("data") or body.get("models") or []
        ids: list[str] = []
        for item in items:
            if isinstance(item, dict):
                candidate = item.get("id") or item.get("slug")
                if isinstance(candidate, str):
                    ids.append(candidate)
            elif isinstance(item, str):
                ids.append(item)
        return ids

    # -- roles ---------------------------------------------------------------- #

    def roles(self, *, provider: str | None = None) -> list[RoleInfo]:
        """Every specialist the engine can run, with its permissions and default model.

        ``mandatory`` reflects what a *run* requires, not a property of the spec: the engine's
        coverage gate demands these roles report, so a plan that omits one cannot pass. The
        UI needs this to show a reviewer as required rather than offering a switch that the
        engine would not honour.
        """
        registry = build_default_registry()
        mandatory = set(registry.mandatory_keys())
        router = self._router(provider)
        # Ask the factory, not the router: the factory is what the engine actually builds
        # agents with, and it knows things a bare router call does not (e.g. that the
        # adversarial reviewer and the verifier resolve through their own tier
        # overrides). Reading the same source here is what stops this table from
        # disagreeing with the models a run reports.
        factory = AgentFactory(self._settings(provider), registry, router=router) if router is not None else None
        infos: list[RoleInfo] = []
        for key in sorted(registry.specs):
            spec = registry.specs[key]
            policy = registry.policies.get(key)
            default_model = ""
            if router is not None and factory is not None:
                default_model = factory.model_for(key)
            infos.append(
                RoleInfo(
                    role_key=key,
                    agent_name=spec.agent_name,
                    description=spec.description,
                    focus=[str(item) for item in spec.focus],
                    may_mutate=policy.may_mutate,
                    allowed_tools=sorted(policy.allowed_tools),
                    mandatory=key in mandatory,
                    planner_selectable=spec.selectable_by_planner,
                    default_model=default_model,
                    tier=role_tier(key, write_capable=policy.may_mutate),
                )
            )
        return infos

    # -- diagnostics ---------------------------------------------------------- #

    async def doctor(self, *, provider: str | None = None, model: str | None = None) -> DoctorResponse:
        """Live capability probe. Makes real, billable model calls - never implicit."""
        settings = Settings.from_env()
        if provider:
            settings = settings.model_copy(update={"provider": provider})
        unset = not settings.provider or settings.provider == "auto"
        if unset and self._router() is None:
            raise ProviderUnavailableError("No provider credential is available to check.")
        report = await run_doctor(settings, provider=provider, model=model)
        return DoctorResponse(
            requested_provider=str(report.get("requested_provider")),
            active_provider=report.get("active_provider"),
            passed=int(report.get("passed", 0)),
            total=int(report.get("total", 0)),
            checks=[
                DoctorCheck(
                    provider=item["provider"],
                    model=item["model"],
                    check=item["check"],
                    ok=bool(item["ok"]),
                    detail=str(item.get("detail", "")),
                )
                for item in report.get("checks", [])
            ],
            note=str(report.get("note", "")),
        )

    def validate_model_selection(self, selection: str, *, provider: str | None = None) -> PreflightCheck:
        """Check one ``provider:model`` choice against what the backend can serve.

        ``blocking`` describes whether *this check failing* would stop a run, not whether it
        passed: a model the user deliberately selected must be servable (blocking), while a
        model the application simply has not catalogued is a warning (advisory), because an
        OpenAI-compatible gateway accepts ids this application has never seen.

        Format is validated before availability: a malformed reference is a different failure
        from a model the account cannot use, and conflating them sends the user to the wrong
        place.
        """
        value = (selection or "").strip()
        if not value or value.lower() == "auto":
            return PreflightCheck(name="model_selection", ok=True, detail="auto routing")
        provider_name, _, model_id = value.partition(":")
        if not model_id:
            provider_name, model_id = (provider or "auto"), value
        if provider_name not in KNOWN_PROVIDERS and provider_name != "auto":
            return PreflightCheck(
                name="model_selection",
                ok=False,
                detail=f"unknown provider '{provider_name}' in '{value}'; expected one of {', '.join(KNOWN_PROVIDERS)}",
            )
        router = self._router(provider if provider_name == "auto" else provider_name)
        if router is None:
            return PreflightCheck(
                name="model_selection",
                ok=False,
                detail=f"provider '{provider_name}' has no usable credential",
            )
        known = {model.id for model in self.models(provider=provider_name)}
        if model_id not in known:
            # Not blocking: the router accepts any model id on an API-compatible gateway,
            # so an unlisted id may still work. It is worth telling the user it is unlisted.
            return PreflightCheck(
                name="model_selection",
                ok=True,
                blocking=False,
                detail=f"'{model_id}' is not in the models reported by {provider_name}; it may still work",
            )
        return PreflightCheck(name="model_selection", ok=True, detail=f"{provider_name}:{model_id}")

    # -- preflight ------------------------------------------------------------ #

    async def preflight(self, request: PreflightRequest, *, source_exists: bool, source_name: str) -> PreflightResult:
        """Decide whether a run is worth starting, with reasons a user can act on."""
        checks: list[PreflightCheck] = []

        checks.append(
            PreflightCheck(
                name="source",
                ok=source_exists,
                detail=f"source '{source_name}' is available" if source_exists else "the selected source no longer exists",
            )
        )
        checks.append(
            PreflightCheck(
                name="objective",
                ok=bool(request.objective.strip()),
                detail="objective is set" if request.objective.strip() else "an objective is required",
            )
        )

        mode = request.config.mode
        if mode == "scripted" and not self.app_settings.allow_scripted_runs:
            checks.append(
                PreflightCheck(
                    name="scripted_mode", ok=False, detail="scripted runs are disabled by the application configuration"
                )
            )

        router = self._router(None if request.config.provider == "auto" else request.config.provider)
        if mode == "live":
            if router is None:
                checks.append(
                    PreflightCheck(
                        name="provider",
                        ok=False,
                        detail=(
                            f"provider '{request.config.provider}' has no usable credential; "
                            "sign in to a provider or switch the run to the scripted engine"
                        ),
                    )
                )
            else:
                entry = next(
                    (item for item in self.status(provider=request.config.provider).providers if item.name == router.default_provider),
                    None,
                )
                checks.append(
                    PreflightCheck(
                        name="provider",
                        ok=True,
                        detail=(
                            f"{router.default_provider} via {entry.auth_source if entry else 'credential store'}"
                            + (" (streaming transport required)" if entry and entry.requires_streaming else "")
                        ),
                    )
                )
        else:
            checks.append(
                PreflightCheck(
                    name="provider",
                    ok=True,
                    blocking=False,
                    detail="scripted engine: no provider credential is needed and no model calls are made",
                )
            )

        for role, selection in request.config.role_models.items():
            check = self.validate_model_selection(selection, provider=request.config.provider)
            checks.append(check.model_copy(update={"name": f"model:{role}"}))

        if request.deep and router is not None and mode == "live":
            try:
                report = await self.doctor(provider=request.config.provider)
                failing = [item for item in report.checks if not item.ok]
                checks.append(
                    PreflightCheck(
                        name="provider_doctor",
                        ok=not failing,
                        detail=(
                            f"{report.passed}/{report.total} capability checks passed"
                            if not failing
                            else "failed: " + "; ".join(f"{item.model} {item.check}" for item in failing[:4])
                        ),
                    )
                )
            except Exception as exc:
                checks.append(
                    PreflightCheck(name="provider_doctor", ok=False, blocking=False, detail=f"probe failed: {exc}")
                )

        blocking = [check.name for check in checks if not check.ok and check.blocking]
        return PreflightResult(ok=not blocking, checks=checks, blocking_failures=blocking)
