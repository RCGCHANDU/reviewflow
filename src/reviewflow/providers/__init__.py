"""Provider plug-and-play: credentials, routing, and model construction.

The framework reaches a model backend through exactly one seam - the SDK's
``ModelProvider`` - and this package supplies it:

* :mod:`.codex_oauth` - OpenAI ChatGPT OAuth (the Codex backend), including token
  refresh and the account headers the endpoint requires;
* :mod:`.commandcode` - a Chat Completions gateway credential read from the
  commandcode CLI's own login store;
* :mod:`.openai_key` - the ordinary API-key path;
* :mod:`.routing` - :class:`ModelRouter`, which turns any of them into the provider
  the Agents SDK drives, so no agent, prompt or stage knows which backend is in use.

Nothing here asks the user to paste a secret: credentials are discovered from the
environment or from stores the vendor CLI already wrote.
"""

from __future__ import annotations

from reviewflow.providers.routing import (
    AUTO_ORDER,
    KNOWN_PROVIDERS,
    ModelRef,
    ModelRouter,
    ProviderProfile,
    RoleModels,
    all_profiles,
    codex_profile,
    commandcode_profile,
    credential_status,
    openai_profile,
    parse_model_ref,
)

__all__ = [
    "AUTO_ORDER",
    "KNOWN_PROVIDERS",
    "ModelRef",
    "ModelRouter",
    "ProviderProfile",
    "RoleModels",
    "all_profiles",
    "codex_profile",
    "commandcode_profile",
    "credential_status",
    "openai_profile",
    "parse_model_ref",
]
