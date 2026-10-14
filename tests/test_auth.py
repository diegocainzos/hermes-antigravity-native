import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from hermes_antigravity_native import auth, constants as C


def write(path, obj):
    path.write_text(json.dumps(obj))


def test_reuses_hermes_google_oauth_when_no_native_store(_isolated_env):
    write(_isolated_env / "auth" / "google_oauth.json", {"access": "A", "refresh": "R", "email": "e@x.com", "expires": 123})
    c = auth.load_credentials()
    assert (c.refresh, c.access, c.email, c.source) == ("R", "A", "e@x.com", "hermes-google-oauth")
    assert not c.is_fresh()


def test_native_store_takes_precedence(_isolated_env):
    write(_isolated_env / "auth" / "google_oauth.json", {"refresh": "OLD"})
    write(_isolated_env / "auth" / "antigravity_native.json", {"refresh": "NEW", "projectId": "pp"})
    c = auth.load_credentials()
    assert c.refresh == "NEW" and c.project_id == "pp" and c.source == "native"


def test_agy_token_file_fallback(tmp_path, monkeypatch):
    f = tmp_path / "tok"
    write(f, {"token": {"access_token": "a", "refresh_token": "r", "expiry": "2030-01-01T00:00:00Z"}})
    monkeypatch.setenv("ANTIGRAVITY_AGY_TOKEN_FILE", str(f))
    c = auth.load_credentials()
    assert c.refresh == "r" and c.source == "agy-cli-token" and c.expires > time.time() * 1000


def test_no_credentials():
    assert auth.load_credentials() is None
    with pytest.raises(auth.AuthError, match="No Antigravity credentials"):
        auth.get_valid_credentials()


def test_env_refresh_token(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_REFRESH_TOKEN", "envR")
    assert auth.load_credentials().source == "env"


def _token_client(status=200, body=None, seen=None):
    def handler(req: httpx.Request):
        if seen is not None:
            seen.append(parse_qs(req.content.decode()))
        return httpx.Response(status, json=body if body is not None else {"access_token": "NEWACC", "expires_in": 3600})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_access_token_request_and_expiry_skew():
    seen = []
    old = auth.Credentials(refresh="R", email="e@x.com", project_id="p")
    out = auth.refresh_access_token(old, client=_token_client(seen=seen), now_ms=1_000_000)
    form = seen[0]
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["R"]
    assert form["client_id"] == [C.client_id()] and form["client_secret"] == [C.client_secret()]
    assert out.access == "NEWACC" and out.refresh == "R" and out.project_id == "p"
    assert out.expires == 1_000_000 + 3600 * 1000 - C.TOKEN_SKEW_S * 1000


def test_refresh_rotated_token_and_error_is_redacted():
    out = auth.refresh_access_token(auth.Credentials(refresh="R"), client=_token_client(body={"access_token": "a", "expires_in": 1, "refresh_token": "R2"}))
    assert out.refresh == "R2"
    with pytest.raises(auth.AuthError) as ei:
        auth.refresh_access_token(auth.Credentials(refresh="R"), client=_token_client(400, {"error": "invalid_grant", "error_description": "Bad ya29.SECRETTOKEN"}))
    assert "invalid_grant" in str(ei.value) and "SECRETTOKEN" not in str(ei.value)


def test_get_valid_credentials_refreshes_and_persists_only_native(_isolated_env):
    gpath = _isolated_env / "auth" / "google_oauth.json"
    write(gpath, {"access": "stale", "refresh": "R", "email": "e@x.com", "expires": 1})
    before = gpath.read_text()
    c = auth.get_valid_credentials(client=_token_client())
    assert c.access == "NEWACC" and c.email == "e@x.com"
    assert gpath.read_text() == before  # foreign store untouched
    stored = json.loads(auth.native_store_path().read_text())
    assert stored["access"] == "NEWACC" and stored["refresh"] == "R"
    assert oct(auth.native_store_path().stat().st_mode & 0o777) == "0o600"
    # second call uses the fresh stored token, no network
    def boom(req):
        raise AssertionError("network used")
    assert auth.get_valid_credentials(client=httpx.Client(transport=httpx.MockTransport(boom))).access == "NEWACC"


def test_pkce_and_auth_url():
    v, ch = auth.generate_pkce()
    import base64, hashlib
    assert ch == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    q = parse_qs(urlparse(auth.build_auth_url("st", ch)).query)
    assert q["redirect_uri"] == [C.REDIRECT_URI] and q["access_type"] == ["offline"] and q["state"] == ["st"]
    assert q["code_challenge_method"] == ["S256"] and "aicode" in q["scope"][0]


@pytest.mark.parametrize("pasted", [
    "http://localhost:51121/oauth-callback?state=S&code=C",
    "  http://localhost:51121/oauth-callback?code=C&state=S&scope=x ",
    "state=S&code=C",
    "?state=S&code=C",
])
def test_parse_pasted_callback_ok(pasted):
    assert auth.parse_pasted_callback(pasted, "S") == ("C", "S")


@pytest.mark.parametrize("pasted,msg", [
    ("", "No callback pasted"),
    ("http://localhost:51121/oauth-callback?state=S", "missing 'code'"),
    ("http://localhost:51121/oauth-callback?state=X&code=C", "State mismatch"),
    ("http://localhost:51121/oauth-callback?error=access_denied", "access_denied"),
])
def test_parse_pasted_callback_errors(pasted, msg):
    with pytest.raises(auth.AuthError, match=msg):
        auth.parse_pasted_callback(pasted, "S")


def test_exchange_code_requires_refresh_token():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "a", "expires_in": 5})))
    with pytest.raises(auth.AuthError, match="No refresh token"):
        auth.exchange_code("c", "v", client=c)


def test_login_with_pasted_callback(monkeypatch):
    seen = {}
    def fake_exchange(code, verifier, client=None):
        seen["code"] = code
        return auth.Credentials(refresh="RR", access="AA", expires=1, email="a@b.c")
    monkeypatch.setattr(auth, "exchange_code", fake_exchange)
    out = []
    state_holder = {}
    def notify(msg):
        if "accounts.google.com" in msg:
            state_holder["s"] = parse_qs(urlparse(msg.strip().split()[-1]).query)["state"][0]
        out.append(msg)
    def prompt(_):
        return f"http://localhost:51121/oauth-callback?code=PASTED&state={state_holder['s']}"
    creds = auth.login(prompt=prompt, notify=notify, listen=False)
    assert seen["code"] == "PASTED" and creds.refresh == "RR"
    assert json.loads(auth.native_store_path().read_text())["refresh"] == "RR"
