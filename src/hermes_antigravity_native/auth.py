"""OAuth + credential store (port of auth/oauth.ts).

Credential sources, first usable wins (``ANTIGRAVITY_NATIVE_SOURCE`` forces one):

1. ``ANTIGRAVITY_REFRESH_TOKEN`` env
2. ``$HERMES_HOME/auth/antigravity_native.json`` — this plugin's own store, written
   ONLY by ``hermes antigravity-native login`` (and its refreshes)
3. ``~/.gemini/antigravity-cli/antigravity-oauth-token`` — agy CLI login (read-only)
4. ``$HERMES_HOME/antigravity-accounts.json`` — old hermes-antigravity-auth store (read-only)
5. ``$HERMES_HOME/auth/google_oauth.json`` — last resort (read-only)

Every source remembers the OAuth client that issued its refresh token (agy and
pi use Google's public Antigravity client), so tokens are refreshed in-process
with the right client. Refreshed access tokens of foreign sources go to a
fingerprint-keyed cache (``antigravity_native_cache.json``) — never into the own
store — so a cached token can never shadow a better source.
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
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import constants as C
from .security import redact_secrets

logger = logging.getLogger(__name__)

NATIVE_STORE_VERSION = 2


class AuthError(RuntimeError):
    pass


hermes_home = C.hermes_home


def native_store_path() -> Path:
    return hermes_home() / "auth" / "antigravity_native.json"


def token_cache_path() -> Path:
    return hermes_home() / "auth" / "antigravity_native_cache.json"


def hermes_google_oauth_path() -> Path:
    return hermes_home() / "auth" / "google_oauth.json"


def agy_token_path() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_AGY_TOKEN_FILE") or Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token")


def antigravity_accounts_path() -> Path:
    """The old hermes-antigravity-auth account store."""
    return hermes_home() / "antigravity-accounts.json"


@dataclass
class Credentials:
    refresh: str
    access: str = ""
    expires: int = 0  # epoch milliseconds (already skew-adjusted)
    email: str | None = None
    project_id: str | None = None
    source: str = "native"
    client_id: str | None = None  # OAuth client that issued ``refresh`` (None = login client)

    def is_fresh(self, now_ms: int | None = None) -> bool:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return bool(self.access) and self.expires > now_ms

    def is_usable(self, now_ms: int | None = None) -> bool:
        """Not fresh enough to skip a refresh, but not actually expired yet either."""
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return bool(self.access) and self.expires + C.TOKEN_SKEW_S * 1000 > now_ms


# ── stores ────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _parse_iso_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _skewed(expires_ms: Any) -> int:
    """Raw expiry (epoch ms) from a foreign store -> skew-adjusted ``Credentials.expires``."""
    try:
        raw = int(expires_ms or 0)
    except (TypeError, ValueError):
        return 0
    return raw - C.TOKEN_SKEW_S * 1000 if raw else 0


def jwt_claims(id_token: str | None) -> dict[str, Any]:
    """Payload of an id_token, unverified (only used to label the issuing client / account)."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def jwt_audience(id_token: str | None) -> str | None:
    """OAuth client that issued an id_token (``azp``/``aud``)."""
    claims = jwt_claims(id_token)
    aud = claims.get("azp") or claims.get("aud")
    return aud if isinstance(aud, str) and aud else None


def parse_native_store(d: dict[str, Any]) -> Credentials | None:
    # Stores written before v2 may hold a copy of another source's token
    # (possibly from a client whose project 403s); ignore them.
    if not d.get("refresh") or not d.get("clientId"):
        return None
    return Credentials(
        refresh=str(d["refresh"]),
        access=str(d.get("access") or ""),
        expires=int(d.get("expires") or 0),
        email=d.get("email"),
        project_id=d.get("projectId") or d.get("project_id"),
        source="native",
        client_id=str(d["clientId"]),
    )


def parse_hermes_antigravity_accounts(d: dict[str, Any]) -> Credentials | None:
    """Parse the old hermes-antigravity-auth account store.

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
    legacy = C.legacy_plugin_client()
    return Credentials(
        refresh=str(account["refreshToken"]),
        access=str(account.get("accessToken") or ""),
        expires=_skewed(account.get("accessTokenExpiresAt")),
        email=account.get("email"),
        project_id=account.get("projectId") or account.get("managedProjectId") or None,
        source="hermes-antigravity-accounts",
        client_id=legacy[0] if legacy else None,
    )


def parse_hermes_google_oauth(d: dict[str, Any]) -> Credentials | None:
    if not d.get("refresh"):
        return None
    return Credentials(
        refresh=str(d["refresh"]),
        access=str(d.get("access") or ""),
        expires=_skewed(d.get("expires")),
        email=d.get("email"),
        project_id=d.get("projectId") or d.get("project_id"),
        source="hermes-google-oauth",
    )


def parse_agy_token(d: dict[str, Any]) -> Credentials | None:
    tok = d.get("token")
    if not isinstance(tok, dict) or not tok.get("refresh_token"):
        return None
    raw_expiry = _parse_iso_ms(str(tok.get("expiry") or ""))
    email = jwt_claims(d.get("id_token")).get("email")
    return Credentials(
        refresh=str(tok["refresh_token"]),
        access=str(tok.get("access_token") or ""),
        expires=_skewed(raw_expiry),
        email=email if isinstance(email, str) else None,
        source="agy-cli-token",
        client_id=jwt_audience(d.get("id_token")),
    )


# (source name, path getter, parser) in priority order.
SOURCES: tuple[tuple[str, Callable[[], Path], Callable[[dict[str, Any]], Credentials | None]], ...] = (
    ("native", native_store_path, parse_native_store),
    # agy's login uses the same public client as pi, and its project has the
    # Cloud Code API enabled — so it streams and we can refresh it in-process.
    ("agy-cli-token", agy_token_path, parse_agy_token),
    # Old plugin's client 1052…: refreshes fine, but inference 403s (API disabled
    # in that client's GCP project). Only used when nothing better exists.
    ("hermes-antigravity-accounts", antigravity_accounts_path, parse_hermes_antigravity_accounts),
    ("hermes-google-oauth", hermes_google_oauth_path, parse_hermes_google_oauth),
)
SOURCE_NAMES = ("env",) + tuple(s[0] for s in SOURCES)


def load_credentials(source: str | None = None) -> Credentials | None:
    """First usable credentials in priority order (or only ``source`` / ``ANTIGRAVITY_NATIVE_SOURCE``)."""
    forced = (source or C.env("NATIVE_SOURCE") or "").strip() or None
    if forced and forced not in SOURCE_NAMES:
        raise AuthError(f"Unknown credential source {forced!r}. Valid: {', '.join(SOURCE_NAMES)}")
    env_refresh = C.env("REFRESH_TOKEN")
    if env_refresh and forced in (None, "env"):
        return Credentials(refresh=env_refresh, email=C.env("EMAIL"), project_id=C.env("PROJECT_ID"),
                           source="env", client_id=C.env("CLIENT_ID"))
    for name, path_fn, parser in SOURCES:
        if forced and name != forced:
            continue
        data = _read_json(path_fn())
        creds = parser(data) if data else None
        if creds:
            return creds
    return None


def save_credentials(creds: Credentials) -> None:
    """Write the plugin's own store. Only for credentials from our own login."""
    _write_private_json(native_store_path(), {
        "version": NATIVE_STORE_VERSION,
        "refresh": creds.refresh,
        "access": creds.access,
        "expires": creds.expires,
        "email": creds.email,
        "projectId": creds.project_id,
        "clientId": creds.client_id or C.client_id(),
    })


# ── token cache (foreign sources) ─────────────────────────────────────────

def fingerprint(refresh: str) -> str:
    return hashlib.sha256(refresh.encode()).hexdigest()[:16]


def _read_cache() -> dict[str, Any]:
    return _read_json(token_cache_path()) or {}


def apply_cache(creds: Credentials) -> Credentials:
    """Overlay a cached access token / project id for exactly this refresh token."""
    hit = _read_cache().get(fingerprint(creds.refresh))
    if not isinstance(hit, dict):
        return creds
    out = replace(creds, project_id=creds.project_id or hit.get("projectId") or None,
                  email=creds.email or hit.get("email") or None)
    if int(hit.get("expires") or 0) > out.expires and hit.get("access"):
        out.access, out.expires = str(hit["access"]), int(hit["expires"])
    return out


def remember(creds: Credentials) -> None:
    """Persist refreshed access token / discovered project for ``creds``' own source only."""
    try:
        if creds.source == "native":
            save_credentials(creds)
            return
        now = int(time.time() * 1000)
        cache = {k: v for k, v in _read_cache().items()
                 if isinstance(v, dict) and (int(v.get("expires") or 0) > now or v.get("projectId"))}
        cache[fingerprint(creds.refresh)] = {
            "source": creds.source, "access": creds.access, "expires": creds.expires,
            "projectId": creds.project_id, "email": creds.email,
        }
        _write_private_json(token_cache_path(), cache)
    except OSError as exc:
        logger.warning("could not persist Antigravity token cache: %s", exc)


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


def oauth_client_for(creds: Credentials) -> tuple[str, str]:
    """(client_id, client_secret) able to refresh ``creds``."""
    if not creds.client_id:
        return C.client_id(), C.client_secret()
    secret = C.known_oauth_clients().get(creds.client_id)
    if not secret:
        raise AuthError(
            f"{creds.source} token was issued by OAuth client {C.client_project_number(creds.client_id)}…, "
            "whose secret is unknown, so it cannot be refreshed in-process."
        )
    return creds.client_id, secret


def refresh_access_token(creds: Credentials, *, client: httpx.Client | None = None, now_ms: int | None = None) -> Credentials:
    """Exchange the refresh token for a fresh access token (same source/client)."""
    cid, secret = oauth_client_for(creds)
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        resp = client.post(
            C.TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": secret,
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
    return replace(
        creds,
        refresh=data.get("refresh_token") or creds.refresh,
        access=data["access_token"],
        expires=now_ms + int(data.get("expires_in", 3600)) * 1000 - C.TOKEN_SKEW_S * 1000,
    )


def expired_message(creds: Credentials, cause: Exception) -> str:
    """Actionable text for "access token expired and refresh failed"."""
    fix = "run `hermes antigravity-native login` once (on a headless box paste the callback URL), then retry"
    if creds.source == "agy-cli-token":
        fix += " — or run `agy` once to re-issue its token"
    return (f"Antigravity access token expired and could not be refreshed in-process "
            f"(source={creds.source}: {redact_secrets(cause)}). Next: {fix}.")


_lock = threading.Lock()


def get_valid_credentials(*, client: httpx.Client | None = None, force_refresh: bool = False) -> Credentials:
    """Return fresh credentials, refreshing in-process when needed."""
    with _lock:
        creds = load_credentials()
        if creds is None:
            raise AuthError(
                "No Antigravity credentials found. Next: run `hermes antigravity-native login` "
                "(or log in with the agy CLI once)."
            )
        creds = apply_cache(creds)
        if creds.is_fresh() and not force_refresh:
            return creds
        try:
            refreshed = refresh_access_token(creds, client=client)
        except AuthError as exc:
            if creds.is_usable() and not force_refresh:
                logger.warning("antigravity refresh failed (%s); using stored access token until it expires", exc)
                return creds
            raise AuthError(expired_message(creds, exc)) from exc
        if refreshed.email is None:
            refreshed.email = fetch_user_email(refreshed.access, client=client)
        remember(refreshed)
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
            source="native",
            client_id=C.client_id(),
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
