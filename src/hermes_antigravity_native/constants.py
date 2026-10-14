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
LOCAL_API_KEY_PLACEHOLDER = "antigravity-native-local"


def env(name: str) -> str | None:
    """Read ``ANTIGRAVITY_<name>`` (legacy ``NOAGY_<name>`` also honoured)."""
    return os.environ.get(f"ANTIGRAVITY_{name}") or os.environ.get(f"NOAGY_{name}") or None


_OLD_PLUGIN_CREDS: dict[str, str] | None = None


def _old_plugin_client_credentials() -> dict[str, str]:
    """Read the hermes-antigravity-auth OAuth client credentials file, if present.

    The refresh tokens that actually work were issued to this client
    (e.g. ``~/.hermes/antigravity-credentials.json``), so it must win over the
    generic pi-antigravity default client id/secret.
    """
    global _OLD_PLUGIN_CREDS
    if _OLD_PLUGIN_CREDS is None:
        path = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "antigravity-credentials.json"
        try:
            data = json.loads(path.read_text())
            _OLD_PLUGIN_CREDS = {
                "client_id": str(data.get("client_id") or ""),
                "client_secret": str(data.get("client_secret") or ""),
            }
        except (OSError, ValueError, AttributeError):
            _OLD_PLUGIN_CREDS = {"client_id": "", "client_secret": ""}
    return _OLD_PLUGIN_CREDS


def client_id() -> str:
    """Env override → hermes-antigravity-auth client file → pi-antigravity default."""
    return env("CLIENT_ID") or _old_plugin_client_credentials().get("client_id") or _DEFAULT_CLIENT_ID


def client_secret() -> str:
    """Env override → hermes-antigravity-auth client file → pi-antigravity default."""
    return env("CLIENT_SECRET") or _old_plugin_client_credentials().get("client_secret") or _DEFAULT_CLIENT_SECRET
