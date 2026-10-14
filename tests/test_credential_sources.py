"""Credential source ordering, per-source OAuth clients, token cache and refresh fallback."""

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from hermes_antigravity_native import auth, constants as C, service
from hermes_antigravity_native import stream as S

LEGACY = ("1052827114944-legacy.apps.googleusercontent.com", "legacy-secret")
AGY_CLIENT = C._DEFAULT_CLIENT_ID


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def id_token(aud):
    payload = base64.urlsafe_b64encode(json.dumps({"aud": aud, "azp": aud, "email": "agy@x.com"}).encode()).rstrip(b"=").decode()
    return f"hdr.{payload}.sig"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def agy_file(tmp_path, monkeypatch):
    f = tmp_path / "agy-token"
    monkeypatch.setenv("ANTIGRAVITY_AGY_TOKEN_FILE", str(f))

    def make(access="agyA", refresh="agyR", expiry_s=None, aud=AGY_CLIENT):
        expiry = iso(expiry_s if expiry_s is not None else time.time() + 3600)
        write(f, {"token": {"access_token": access, "refresh_token": refresh, "expiry": expiry},
                  "auth_method": "consumer", "id_token": id_token(aud)})
        return f
    return make


def legacy_store(home):
    write(home / "antigravity-credentials.json", {"client_id": LEGACY[0], "client_secret": LEGACY[1]})
    write(home / "antigravity-accounts.json", {"activeIndex": 0, "accounts": [{
        "email": "e@x.com", "refreshToken": "legacyR", "accessToken": "legacyA",
        "accessTokenExpiresAt": int(time.time() * 1000) + 3_600_000, "projectId": "gen-lang-client-1"}]})


def token_client(seen, status=200, body=None):
    def handler(req):
        seen.append(parse_qs(req.content.decode()))
        return httpx.Response(status, json=body if body is not None else {"access_token": "FRESH", "expires_in": 3600})
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── ordering ──────────────────────────────────────────────────────────────

def test_agy_wins_over_old_plugin_accounts(_isolated_env, agy_file):
    legacy_store(_isolated_env)
    agy_file()
    c = auth.load_credentials()
    assert c.source == "agy-cli-token" and c.client_id == AGY_CLIENT and c.email == "agy@x.com"


def test_own_login_store_wins_over_agy(_isolated_env, agy_file):
    agy_file()
    write(auth.native_store_path(), {"version": 2, "refresh": "own", "clientId": C.client_id()})
    assert auth.load_credentials().source == "native"


def test_legacy_native_store_without_client_marker_is_ignored(_isolated_env, agy_file):
    """Pre-v2 stores could hold a copied/1052 token that 403s; they must not shadow agy."""
    agy_file()
    write(auth.native_store_path(), {"refresh": "copied", "access": "x", "expires": 2**50, "projectId": "aicode-consumers"})
    assert auth.load_credentials().source == "agy-cli-token"


def test_accounts_used_with_legacy_client_when_alone(_isolated_env):
    legacy_store(_isolated_env)
    c = auth.load_credentials()
    assert c.source == "hermes-antigravity-accounts" and c.client_id == LEGACY[0] and c.project_id == "gen-lang-client-1"


def test_forced_source_env(_isolated_env, agy_file, monkeypatch):
    legacy_store(_isolated_env)
    agy_file()
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_SOURCE", "hermes-antigravity-accounts")
    assert auth.load_credentials().source == "hermes-antigravity-accounts"
    assert auth.load_credentials(source="agy-cli-token").source == "agy-cli-token"
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_SOURCE", "bogus")
    with pytest.raises(auth.AuthError, match="Unknown credential source"):
        auth.load_credentials()


def test_forced_source_missing_returns_none(_isolated_env, agy_file):
    agy_file()
    assert auth.load_credentials(source="native") is None


# ── clients ───────────────────────────────────────────────────────────────

def test_login_client_ignores_old_plugin_client_file(_isolated_env):
    legacy_store(_isolated_env)
    assert C.client_id() == C._DEFAULT_CLIENT_ID and C.client_secret() == C._DEFAULT_CLIENT_SECRET
    assert C.known_oauth_clients()[LEGACY[0]] == LEGACY[1]


def test_jwt_audience():
    assert auth.jwt_audience(id_token("abc")) == "abc"
    assert auth.jwt_audience("garbage") is None and auth.jwt_audience(None) is None


def test_agy_expiry_is_skew_adjusted(agy_file):
    exp = time.time() + 3600
    agy_file(expiry_s=exp)
    c = auth.load_credentials()
    assert abs(c.expires - (int(exp) * 1000 - C.TOKEN_SKEW_S * 1000)) < 2000


def test_agy_refreshes_in_process_with_its_own_client(_isolated_env, agy_file):
    f = agy_file(access="old", expiry_s=time.time() - 10)
    before = f.read_text()
    seen = []
    c = auth.get_valid_credentials(client=token_client(seen))
    assert seen[0]["client_id"] == [AGY_CLIENT] and seen[0]["client_secret"] == [C._DEFAULT_CLIENT_SECRET]
    assert seen[0]["refresh_token"] == ["agyR"]
    assert (c.access, c.source, c.client_id) == ("FRESH", "agy-cli-token", AGY_CLIENT)
    assert f.read_text() == before and not auth.native_store_path().exists()


def test_accounts_refresh_uses_legacy_client(_isolated_env):
    legacy_store(_isolated_env)
    seen = []
    auth.get_valid_credentials(client=token_client(seen), force_refresh=True)
    assert seen[0]["client_id"] == [LEGACY[0]] and seen[0]["client_secret"] == [LEGACY[1]]


def test_unknown_client_cannot_refresh_actionable(agy_file):
    agy_file(access="old", expiry_s=time.time() - 3600, aud="999-unknown.apps.googleusercontent.com")
    with pytest.raises(auth.AuthError) as ei:
        auth.get_valid_credentials(client=token_client([]))
    msg = str(ei.value)
    assert "expired" in msg and "hermes antigravity-native login" in msg and "agy" in msg


# ── cache ─────────────────────────────────────────────────────────────────

def test_cache_is_keyed_by_refresh_token(_isolated_env, agy_file):
    agy_file(refresh="R1", access="old", expiry_s=time.time() - 10)
    auth.get_valid_credentials(client=token_client([]))
    # agy re-logged in with a different refresh token: the old cache entry must not apply
    agy_file(refresh="R2", access="old2", expiry_s=time.time() - 10)
    seen = []
    c = auth.get_valid_credentials(client=token_client(seen, body={"access_token": "FRESH2", "expires_in": 3600}))
    assert [s["refresh_token"] for s in seen if "grant_type" in s] == [["R2"]] and c.access == "FRESH2"


def test_fresher_source_token_beats_cache(_isolated_env, agy_file):
    agy_file(access="old", expiry_s=time.time() - 10)
    auth.get_valid_credentials(client=token_client([], body={"access_token": "CACHED", "expires_in": 600}))
    agy_file(access="agyNEW", expiry_s=time.time() + 3600)  # agy refreshed itself meanwhile
    assert auth.get_valid_credentials(client=token_client([])).access == "agyNEW"


def test_resolve_session_remembers_project_in_cache_not_native_store(_isolated_env, agy_file, monkeypatch):
    agy_file()
    calls = []
    monkeypatch.setattr(service.shared_client(), "load_code_assist", lambda tok: calls.append(tok) or "aicode-consumers")
    creds, project = service.resolve_session()
    assert project == "aicode-consumers" and not auth.native_store_path().exists()
    assert json.loads(auth.token_cache_path().read_text())[auth.fingerprint("agyR")]["projectId"] == "aicode-consumers"
    _, project2 = service.resolve_session()
    assert project2 == "aicode-consumers" and len(calls) == 1  # project came from the cache


# ── refresh failure handling ──────────────────────────────────────────────

def test_refresh_failure_uses_still_valid_token(agy_file):
    # inside the skew window: not "fresh", but not actually expired
    agy_file(access="stillvalid", expiry_s=time.time() + 120)
    c = auth.get_valid_credentials(client=token_client([], 400, {"error": "invalid_grant"}))
    assert c.access == "stillvalid"


def test_refresh_failure_with_expired_token_is_actionable(agy_file):
    agy_file(access="dead", expiry_s=time.time() - 60)
    with pytest.raises(auth.AuthError) as ei:
        auth.get_valid_credentials(client=token_client([], 400, {"error": "invalid_grant", "error_description": "ya29.LEAK"}))
    msg = str(ei.value)
    assert "expired" in msg and "invalid_grant" in msg and "LEAK" not in msg and "login" in msg


def test_forced_refresh_failure_never_returns_rejected_token(agy_file):
    agy_file(access="valid-but-rejected", expiry_s=time.time() + 3600)
    with pytest.raises(auth.AuthError):
        auth.get_valid_credentials(client=token_client([], 400, {"error": "invalid_grant"}), force_refresh=True)


def test_service_disabled_403_is_explained():
    msg = S.friendly_error(403, json.dumps({"error": {"message": "Cloud Code Private API has not been used in project 1052827114944 before or it is disabled."}}))
    assert "1052827114944" in msg and "hermes antigravity-native login" in msg
