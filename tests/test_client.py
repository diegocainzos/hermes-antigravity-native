import httpx
import pytest

from hermes_antigravity_native import client as K


def test_extract_project_id_shapes():
    assert K.extract_project_id({"cloudaicompanionProject": "proj-1"}) == "proj-1"
    assert K.extract_project_id({"cloudaicompanionProject": {"id": "proj-2"}}) == "proj-2"
    assert K.extract_project_id({"projects": [{"projectId": "p3"}]}) == "p3"
    assert K.extract_project_id({"projectIds": ["p4"]}) == "p4"
    assert K.extract_project_id({"antigravityProjectId": "a", "projectId": "b"}) == "a"
    assert K.extract_project_id({}) is None and K.extract_project_id("x") is None


def test_stable_project_id_is_uuid_v5_shaped_and_deterministic():
    a = K.stable_project_id("me@x.com")
    assert a == K.stable_project_id("me@x.com") != K.stable_project_id("you@x.com")
    assert a[14] == "5" and a[19] in "89ab" and len(a) == 36


def test_project_env_override(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PROJECT_ID", " forced ")
    assert K.resolve_project_id(credential_project_id="c", warmed="w") == "forced"


def test_resolve_project_precedence():
    assert K.resolve_project_id(credential_project_id="c", warmed="w") == "w"
    assert K.resolve_project_id(credential_project_id="c") == "c"
    assert K.resolve_project_id(email="a@b.c") == K.stable_project_id("a@b.c")


def test_endpoint_candidates(monkeypatch):
    assert K.endpoint_candidates()[0] == "https://daily-cloudcode-pa.googleapis.com" and len(K.endpoint_candidates()) == 3
    monkeypatch.setenv("ANTIGRAVITY_BASE_URL", "https://x.googleapis.com/")
    assert K.endpoint_candidates() == ["https://x.googleapis.com"]
    monkeypatch.setenv("ANTIGRAVITY_BASE_URL", "https://evil.example")
    with pytest.raises(ValueError):
        K.endpoint_candidates()


def test_headers():
    h = K.antigravity_headers("tok")
    assert h["Authorization"] == "Bearer tok" and h["User-Agent"].startswith("antigravity/cli/")


def _client(handler):
    return K.AntigravityClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_load_code_assist_falls_through_endpoints_and_caches():
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if "daily-cloudcode-pa.googleapis.com" in str(req.url):
            return httpx.Response(503)
        if "sandbox" in str(req.url):
            return httpx.Response(200, json={"cloudaicompanionProject": "disc-1"})
        return httpx.Response(500)

    c = _client(handler)
    assert c.load_code_assist("tokA") == "disc-1"
    n = len(calls)
    assert c.load_code_assist("tokA") == "disc-1" and len(calls) == n  # cached


def test_load_code_assist_falls_back_to_list_projects():
    def handler(req):
        if req.url.path.endswith("loadCodeAssist"):
            return httpx.Response(200, json={"currentTier": {}})
        return httpx.Response(200, json={"projects": [{"id": "listed"}]})

    assert _client(handler).load_code_assist("t") == "listed"


def test_fetch_available_models_merges_and_registers_enums():
    def handler(req):
        if "sandbox" in str(req.url):
            return httpx.Response(200, json={"models": {"gemini-x": {"model": "MODEL_X"}}, "defaultAgentModelId": "gemini-x"})
        if "daily" in str(req.url):
            return httpx.Response(200, json={"models": {"claude-sonnet-4-6": {}, "chat_1": {"isInternal": True}}})
        return httpx.Response(404)

    data = _client(handler).fetch_available_models("t", "p")
    assert set(data["models"]) == {"gemini-x", "claude-sonnet-4-6", "chat_1"}
    assert data["defaultAgentModelId"] == "gemini-x"
    assert K.usable_model_ids(data) == ["claude-sonnet-4-6", "gemini-x"]


def test_fetch_available_models_all_fail_raises_redacted():
    with pytest.raises(RuntimeError, match="fetchAvailableModels failed"):
        _client(lambda r: httpx.Response(403, text="nope")).fetch_available_models("t", "p")
