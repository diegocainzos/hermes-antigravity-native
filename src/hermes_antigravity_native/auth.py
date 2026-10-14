"""OAuth + credential store (port of auth/oauth.ts).

Credential resolution order (first with a usable refresh/access token wins):

1. ``$HERMES_HOME/auth/antigravity_native.json``  (this plugin's own store)
2. ``$HERMES_HOME/auth/google_oauth.json``          (reused read-only: ``access``/``refresh``/``email``/``expires`` ms)
3. ``~/.gemini/antigravity-cli/antigravity-oauth-token`` (agy CLI token file, read-only, read at runtime)

Refreshed credentials are only ever written to (1), so the other stores are
never mutated.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import constants as C
from .security import redact_secrets

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - only when run outside Hermes' venv
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def native_store_path() -> Path:
    return hermes_home() / "auth" / "antigravity_native.json"


def hermes_google_oauth_path() -> Path:
    return hermes_home() / "auth" / "google_oauth.json"


def agy_token_path() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_AGY_TOKEN_FILE") or Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token")


def antigravity_accounts_path() -> Path:
    """The old hermes-antigravity-auth account store (proven-working refresh tokens)."""
    return hermes_home() / "antigravity-accounts.json"


@dataclass
class Credentials:
    refresh: str
    access: str = ""
    expires: int = 0  # epoch milliseconds (already skew-adjusted)
    email: str | None = None
    project_id: str | None = None
    source: str = "native"

    def is_fresh(self, now_ms: int | None = None) -> bool:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return bool(self.access) and self.expires > now_ms


# ── stores ────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _parse_iso_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def parse_native_store(d: dict[str, Any]) -> Credentials | None:
    if not d.get("refresh"):
        return None
    return Credentials(
        refresh=str(d["refresh"]),
        access=str(d.get("access") or ""),
        expires=int(d.get("expires") or 0),
        email=d.get("email"),
        project_id=d.get("projectId") or d.get("project_id"),
        source="native",
    )


def parse_hermes_antigravity_accounts(d: dict[str, Any]) -> Credentials | None:
    """Parse the old hermes-antigravity-auth account store (the proven-working source).

    Shape: {"activeIndex": int, "accounts": [{"email", "refreshToken",
    "projectId", "managedProjectId", "accessToken", "accessTokenExpiresAt"}]}.
    """
    accounts = d.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None
    idx = d.get("activeIndex", 0)
    account = accounts[idx] if isinstance(idx, int) and 0 <= idx < len(accounts) else accounts[0]
    if not isinstance(account, dict) or not account.get("refreshToken"):
        return None
    return Credentials(
        refresh=str(account["refreshToken"]),
        access=str(account.get("accessToken") or ""),
        expires=int(account.get("accessTokenExpiresAt") or 0),
        email=account.get("email"),
        project_id=account.get("projectId") or account.get("managedProjectId") or None,
        source="hermes-antigravity-accounts",
    )


def parse_hermes_google_oauth(d: dict[str, Any]) -> Credentials | None:
    if not d.get("refresh"):
        return None
    return Credentials(
        refresh=str(d["refresh"]),
        access=str(d.get("access") or ""),
        expires=int(d.get("expires") or 0),
        email=d.get("email"),
        project_id=d.get("projectId") or d.get("project_id"),
        source="hermes-google-oauth",
    )


def parse_agy_token(d: dict[str, Any]) -> Credentials | None:
    tok = d.get("token")
    if not isinstance(tok, dict) or not tok.get("refresh_token"):
        return None
    return Credentials(
        refresh=str(tok["refresh_token"]),
        access=str(tok.get("access_token") or ""),
        expires=_parse_iso_ms(str(tok.get("expiry") or "")),
        source="agy-cli-token",
    )


def load_credentials() -> Credentials | None:
    env_refresh = C.env("REFRESH_TOKEN")
    if env_refresh:
        return Credentials(refresh=env_refresh, email=C.env("EMAIL"), project_id=C.env("PROJECT_ID"), source="env")
    for path, parser in (
        (native_store_path(), parse_native_store),
        (antigravity_accounts_path(), parse_hermes_antigravity_accounts),
        (agy_token_path(), parse_agy_token),
        (hermes_google_oauth_path(), parse_hermes_google_oauth),
    ):
        data = _read_json(path)
        creds = parser(data) if data else None
        if creds:
            return creds
    return None


def save_credentials(creds: Credentials) -> None:
    path = native_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "refresh": creds.refresh,
        "access": creds.access,
        "expires": creds.expires,
        "email": creds.email,
        "projectId": creds.project_id,
    }
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ── token endpoint ────────────────────────────────────────────────────────

def sanitize_oauth_error(text: str) -> str:
    red = redact_secrets(text).strip()
    try:
        parsed = json.loads(red)
        parts = [p for p in (parsed.get("error"), parsed.get("error_description")) if isinstance(p, str) and p]
        if parts:
            return ": ".join(parts)[:300]
    except (ValueError, AttributeError):
        pass
    return red[:300] or "unknown OAuth provider error"


def refresh_access_token(creds: Credentials, *, client: httpx.Client | None = None, now_ms: int | None = None) -> Credentials:
    """Exchange the refresh token for a fresh access token."""
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        resp = client.post(
            C.TOKEN_URL,
            data={
                "client_id": C.client_id(),
                "client_secret": C.client_secret(),
                "refresh_token": creds.refresh,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        raise AuthError(f"Antigravity token refresh failed: {sanitize_oauth_error(resp.text)}")
    data = resp.json()
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return Credentials(
        refresh=data.get("refresh_token") or creds.refresh,
        access=data["access_token"],
        expires=now_ms + int(data.get("expires_in", 3600)) * 1000 - C.TOKEN_SKEW_S * 1000,
        email=creds.email,
        project_id=creds.project_id,
        source="native",
    )


_lock = threading.Lock()


def get_valid_credentials(*, client: httpx.Client | None = None, force_refresh: bool = False) -> Credentials:
    """Return fresh credentials, refreshing + persisting to the native store if needed."""
    with _lock:
        creds = load_credentials()
        if creds is None:
            raise AuthError(
                "No Antigravity credentials found. Run `hermes antigravity-native login` "
                "or provide ~/.hermes/auth/google_oauth.json."
            )
        if creds.is_fresh() and not force_refresh:
            return creds
        refreshed = refresh_access_token(creds, client=client)
        if refreshed.email is None:
            refreshed.email = fetch_user_email(refreshed.access, client=client)
        if C.env("REFRESH_TOKEN") is None:
            try:
                save_credentials(refreshed)
            except OSError as exc:
                logger.warning("could not persist refreshed Antigravity token: %s", exc)
        return refreshed


def fetch_user_email(token: str, *, client: httpx.Client | None = None) -> str | None:
    own = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        r = client.get(C.USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        return r.json().get("email") if r.status_code == 200 else None
    except Exception:
        return None
    finally:
        if own:
            client.close()


# ── interactive login (PKCE) ──────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_auth_url(state: str, challenge: str) -> str:
    params = {
        "client_id": C.client_id(),
        "response_type": "code",
        "redirect_uri": C.REDIRECT_URI,
        "scope": " ".join(C.SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{C.AUTH_URL}?{urlencode(params)}"


def parse_pasted_callback(raw: str, expected_state: str) -> tuple[str, str]:
    """Validate a callback URL / bare query string pasted from a remote browser."""
    text = (raw or "").strip()
    if not text:
        raise AuthError(f"No callback pasted. Paste the full URL from your browser's address bar ({C.REDIRECT_URI}?…).")
    if "://" in text or text.startswith("/"):
        query = urlparse(text).query
    else:  # bare query string: "state=…&code=…" or "?state=…&code=…"
        query = text.lstrip("?")
    qs = parse_qs(query)
    error = (qs.get("error") or [None])[0]
    if error:
        raise AuthError(f"OAuth error from browser: {error[:200]}")
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    if not code or not state:
        raise AuthError("Pasted text is missing 'code' or 'state'. Paste the FULL callback URL from your browser's address bar.")
    if state != expected_state:
        raise AuthError("State mismatch: that callback is from a different sign-in. Re-run login and paste the new URL.")
    return code, state


def exchange_code(code: str, verifier: str, *, client: httpx.Client | None = None) -> Credentials:
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        resp = client.post(
            C.TOKEN_URL,
            data={
                "client_id": C.client_id(),
                "client_secret": C.client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": C.REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        if resp.status_code != 200:
            raise AuthError(f"Token exchange failed: {sanitize_oauth_error(resp.text)}")
        data = resp.json()
        if not data.get("refresh_token"):
            raise AuthError("No refresh token received. Re-run login and allow offline access.")
        creds = Credentials(
            refresh=data["refresh_token"],
            access=data["access_token"],
            expires=int(time.time() * 1000) + int(data.get("expires_in", 3600)) * 1000 - C.TOKEN_SKEW_S * 1000,
        )
        creds.email = fetch_user_email(creds.access, client=client)
        return creds
    finally:
        if own:
            client.close()


def _wait_local_callback(expected_state: str, timeout_s: float) -> tuple[str, str] | None:
    """Run the localhost:51121 callback listener; return (code,state) or None on timeout/bind failure."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict[str, tuple[str, str] | Exception] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # silence
            pass

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if u.path != "/oauth-callback":
                self.send_error(404)
                return
            try:
                code, state = parse_pasted_callback(self.path, expected_state)
                body, status = b"Antigravity authentication complete. You can close this window.", 200
                result["ok"] = (code, state)
            except AuthError as exc:
                body, status = f"Antigravity authentication failed: {exc}".encode(), 400
                result["err"] = exc
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            done.set()

    try:
        server = HTTPServer(("127.0.0.1", C.CALLBACK_PORT), Handler)
    except OSError:
        return None
    server.timeout = 0.5
    deadline = time.time() + timeout_s
    try:
        while not done.is_set() and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    val = result.get("ok")
    return val if isinstance(val, tuple) else None


def login(
    *,
    prompt: Callable[[str], str] = input,
    notify: Callable[[str], None] = print,
    listen: bool = True,
    timeout_s: float = C.OAUTH_CALLBACK_TIMEOUT_S,
) -> Credentials:
    """Interactive login. Local listener on :51121 races with a pasted callback URL.

    On a headless box the listener never fires; the user pastes the redirected
    ``http://localhost:51121/oauth-callback?...`` URL from their browser instead.
    """
    verifier, challenge = generate_pkce()
    state = _b64url(secrets.token_bytes(32))
    notify("Open this URL in a browser and sign in with Google:\n\n  " + build_auth_url(state, challenge) + "\n")
    notify(
        "If this machine is remote/headless your browser will fail to load localhost:51121 after sign-in. "
        "That is fine: copy the full URL from the address bar and paste it below."
    )
    code_state: tuple[str, str] | None = None
    box: dict[str, tuple[str, str]] = {}
    stop = threading.Event()
    if listen:
        def _listen() -> None:
            got = _wait_local_callback(state, timeout_s)
            if got:
                box["v"] = got
                stop.set()
        threading.Thread(target=_listen, daemon=True).start()
    while code_state is None:
        if "v" in box:
            code_state = box["v"]
            break
        try:
            text = prompt("Paste callback URL (or wait for the browser redirect): ")
        except EOFError:
            text = ""
        if "v" in box:
            code_state = box["v"]
            break
        try:
            code_state = parse_pasted_callback(text, state)
        except AuthError as exc:
            notify(f"Invalid callback ({exc}). Try again.")
            if not text.strip() and not listen:
                raise
    creds = exchange_code(code_state[0], verifier)
    save_credentials(creds)
    return creds
