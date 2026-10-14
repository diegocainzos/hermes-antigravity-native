import json
import threading

import httpx
import pytest

from hermes_antigravity_native import auth, gateway as G, service
from hermes_antigravity_native import stream as S
from hermes_antigravity_native.stream import Event


def ev_text(*texts):
    for t in texts:
        yield Event("text", text=t)
    yield Event("finish", finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})


def test_extract_effort_variants():
    assert G.extract_effort({"reasoning_effort": "high"}) == "high"
    assert G.extract_effort({"reasoning": {"effort": "low"}}) == "low"
    assert G.extract_effort({"reasoning": {"enabled": False}}) == "none"
    assert G.extract_effort({"extra_body": {"reasoning_effort": "medium"}}) == "medium"
    assert G.extract_effort({}) is None


def test_host_guard():
    assert G.host_is_loopback("127.0.0.1:51789") and G.host_is_loopback("localhost") and G.host_is_loopback("[::1]:80")
    assert not G.host_is_loopback("evil.com") and not G.host_is_loopback("evil.com:51789") and not G.host_is_loopback("")


def test_events_to_chunks_and_collect():
    evs = iter([Event("thinking", text="t"), Event("text", text="a"), Event("text", text="b"),
                Event("tool_call", tool={"id": "x", "name": "ls", "arguments": "{}", "signature": "c2ln"}),
                Event("finish", finish_reason="tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 0, "reasoning_tokens": 0})])
    resp = G.collect_response(G.events_to_chunks(evs, "m"), "m")
    msg = resp["choices"][0]["message"]
    assert msg["content"] == "ab" and msg["reasoning_content"] == "t"
    assert msg["tool_calls"][0]["function"] == {"name": "ls", "arguments": "{}"}
    assert msg["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "c2ln"}}
    assert resp["choices"][0]["finish_reason"] == "tool_calls" and resp["usage"]["total_tokens"] == 3


@pytest.fixture
def server(monkeypatch, _isolated_env):
    """Real loopback server; credentials + upstream stream are faked."""
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_PORT", "0")
    calls = {}

    def fake_session():
        return auth.Credentials(refresh="r", access="acc", expires=2**60), "proj"

    def fake_stream(http, p):
        calls["params"] = p
        p.resolved_model = "gemini-3.8-flash-high"
        return calls["events"](p)

    monkeypatch.setattr(service, "resolve_session", fake_session)
    monkeypatch.setattr(G, "stream_generate", fake_stream)
    calls["events"] = lambda p: ev_text("Hel", "lo")
    srv = G._Server(("127.0.0.1", 0), G.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, calls
    srv.shutdown()
    srv.server_close()


def test_health_and_models(server):
    base, _ = server
    assert httpx.get(base + "/health").json() == {"status": "ok", "server": G.SERVER_ID}
    ids = [m["id"] for m in httpx.get(base + "/v1/models").json()["data"]]
    assert "gemini-3.8-flash-high" in ids and "claude-opus-4-6-thinking" in ids and len(ids) == 6


def test_non_stream_chat_and_param_mapping(server):
    base, calls = server
    r = httpx.post(base + "/v1/chat/completions", json={
        "model": "antigravity-native/gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high", "max_tokens": 77, "temperature": 0.5})
    assert r.status_code == 200
    j = r.json()
    assert j["choices"][0]["message"]["content"] == "Hello" and j["usage"]["total_tokens"] == 5
    p = calls["params"]
    assert (p.model_id, p.effort, p.max_tokens, p.temperature, p.token, p.project_id) == ("gemini-3.8-flash", "high", 77, 0.5, "acc", "proj")


def test_streaming_chat_is_sse_ending_with_done(server):
    base, _ = server
    with httpx.stream("POST", base + "/v1/chat/completions", json={"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [l for l in r.iter_lines() if l.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[5:]) for l in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks) == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_upstream_quota_error_maps_to_429_openai_error(server):
    base, calls = server

    def boom(p):
        raise S.AntigravityAPIError("Quota reached. Please wait 1h. Ya29.x", 429)
        yield  # pragma: no cover

    calls["events"] = boom
    r = httpx.post(base + "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"


def test_auth_error_maps_to_401(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(service, "resolve_session", lambda: (_ for _ in ()).throw(auth.AuthError("No Antigravity credentials found.")))
    r = httpx.post(base + "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_bad_requests(server):
    base, _ = server
    assert httpx.post(base + "/v1/chat/completions", content=b"{nope").status_code == 400
    assert httpx.post(base + "/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 400
    assert httpx.post(base + "/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 400
    assert httpx.post(base + "/nope", json={}).status_code == 404


def test_rebinding_host_header_rejected(server):
    base, _ = server
    assert httpx.get(base + "/health", headers={"Host": "evil.example"}).status_code == 403


def test_ensure_gateway_reuses_and_stops(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_PORT", "0")
    # port 0 => ephemeral, so just exercise start/stop idempotence
    G.stop_gateway()
    G.ensure_gateway_running()
    assert G._server is not None
    G.ensure_gateway_running()
    G.stop_gateway()
    assert G._server is None
