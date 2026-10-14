"""Hermes ``ProviderProfile`` for the native Antigravity provider."""

from __future__ import annotations

import logging
from typing import Any

from . import constants as C
from . import models as M

logger = logging.getLogger(__name__)


def build_profile():
    from providers.base import ProviderProfile
    from .gateway import ensure_gateway_running, gateway_base_url

    class AntigravityNativeProfile(ProviderProfile):
        """Declares the loopback gateway; starts it lazily inside the Hermes process."""

        def _ensure(self) -> None:
            try:
                ensure_gateway_running()
            except Exception as exc:  # never break provider discovery
                logger.warning("antigravity-native gateway failed to start: %s", exc)

        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self._ensure()  # runs right before every request build
            return messages

        def fetch_models(self, *, api_key=None, base_url=None, timeout: float = 8.0):
            return M.default_model_ids()

        def build_api_kwargs_extras(self, *, reasoning_config=None, **ctx: Any):
            """Hermes reasoning_config -> top-level ``reasoning_effort`` (routes -low/-medium/-high)."""
            if isinstance(reasoning_config, dict):
                if reasoning_config.get("enabled") is False:
                    return {}, {"reasoning_effort": "none"}
                eff = str(reasoning_config.get("effort") or "").strip().lower()
                if eff:
                    return {}, {"reasoning_effort": eff}
            return {}, {}

    return AntigravityNativeProfile(
        name=C.PROVIDER_ID,
        aliases=("antigravity_native", "agy-native"),
        display_name=C.PROVIDER_NAME,
        description="Google Antigravity / Cloud Code Assist — native in-process API (Gemini, Claude, GPT-OSS)",
        signup_url="https://antigravity.google",
        env_vars=(C.PROVIDER_API_KEY_ENV,),
        base_url=gateway_base_url(),
        auth_type="api_key",
        supports_health_check=False,
        supports_vision=True,
        fallback_models=tuple(M.default_model_ids()),
        default_aux_model="gemini-3.8-flash-low",
    )


def register() -> None:
    import os

    from providers import register_provider
    from .gateway import gateway_api_key

    # Hermes' api_key providers need a non-empty key; ours is the loopback gateway's
    # bearer, so `--provider antigravity-native` works without any .env edit.
    try:
        os.environ.setdefault(C.PROVIDER_API_KEY_ENV, gateway_api_key())
    except OSError as exc:
        logger.warning("antigravity-native: could not create the gateway key: %s", exc)
    register_provider(build_profile())
