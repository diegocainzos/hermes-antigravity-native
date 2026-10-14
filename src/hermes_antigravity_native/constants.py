"""Static constants ported from pi-antigravity (client.ts / oauth.ts)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

PROVIDER_ID = "antigravity-native"
PROVIDER_NAME = "Antigravity (native)"

DEFAULT_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
ENDPOINT_FALLBACKS = (
    DEFAULT_ENDPOINT,
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
REDIRECT_URI = "http://localhost:51121/oauth-callback"
CALLBACK_PORT = 51121
OAUTH_CALLBACK_TIMEOUT_S = 5 * 60
SCOPES = (
    "https://www.googleapis.com/auth/aicode",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)

# Google's public Antigravity desktop OAuth client (not a private app secret);
# same values pi-antigravity ships. Overridable via env for custom OAuth apps.
_DEFAULT_CLIENT_ID = base64.b64decode(
    "MTA3MTAwNjA2MDU5MS10bWhzc2luMmgyMWxjcmUyMzV2dG9sb2poNGc0MDNlc"
    "C5hcHBzLmdvb2dsZXVzZXJjb250ZW50LmNvbQ=="
).decode()
_DEFAULT_CLIENT_SECRET = base64.b64decode("R09DU1BYLUs1OEZXUjQ" "4NkxkTEoxbUxCOHNYQzR6NnFEQWY=").decode()

DEFAULT_USER_AGENT = (
    "antigravity/cli/1.1.23 (aidev_client; os_type=linux; arch=amd64; "
    "cl=974125021; auth_method=consumer)"
)

DISCOVERY_TIMEOUT_S = 8.0
PROJECT_CACHE_TTL_S = 30 * 60
# Refresh this long before the access token actually expires.
TOKEN_SKEW_S = 5 * 60

DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 51789
PROVIDER_API_KEY_ENV = "ANTIGRAVITY_NATIVE_API_KEY"  # gateway bearer; see gateway.gateway_api_key()


def env(name: str) -> str | None:
    """Read ``ANTIGRAVITY_<name>`` (legacy ``NOAGY_<name>`` also honoured)."""
    return os.environ.get(f"ANTIGRAVITY_{name}") or os.environ.get(f"NOAGY_{name}") or None


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - only when run outside Hermes' venv
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def legacy_plugin_client() -> tuple[str, str] | None:
    """OAuth client of the old hermes-antigravity-auth plugin (``antigravity-credentials.json``).

    Only used to refresh tokens that plugin issued. Its GCP project does not
    have the Cloud Code API enabled, so those tokens refresh but 403 on inference.
    """
    try:
        data = json.loads((hermes_home() / "antigravity-credentials.json").read_text())
        cid, secret = str(data.get("client_id") or ""), str(data.get("client_secret") or "")
    except (OSError, ValueError, AttributeError):
        return None
    return (cid, secret) if cid and secret else None


def client_id() -> str:
    """Client used for new logins: env override → Google's public Antigravity client (same as agy / pi)."""
    return env("CLIENT_ID") or _DEFAULT_CLIENT_ID


def client_secret() -> str:
    return env("CLIENT_SECRET") or _DEFAULT_CLIENT_SECRET


def known_oauth_clients() -> dict[str, str]:
    """client_id -> client_secret for every OAuth client we can refresh tokens with."""
    out = {_DEFAULT_CLIENT_ID: _DEFAULT_CLIENT_SECRET}
    legacy = legacy_plugin_client()
    if legacy:
        out[legacy[0]] = legacy[1]
    if env("CLIENT_ID") and env("CLIENT_SECRET"):
        out[env("CLIENT_ID")] = env("CLIENT_SECRET")
    return out


def client_project_number(cid: str | None) -> str:
    """Non-secret short label for an OAuth client id (its GCP project number)."""
    return (cid or "").split("-", 1)[0] or "unknown"
