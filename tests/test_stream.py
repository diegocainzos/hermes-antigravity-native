import json

import httpx
import pytest

from hermes_antigravity_native import stream as S


def sse(*objs):
    return "".join(f"data: {json.dumps(o)}\n\n" for o in objs)


TEXT1 = {"response": {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}}
TEXT2 = {"response": {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 10, "cachedContentTokenCount": 4, "candidatesTokenCount": 5,
                                        "thoughtsTokenCount": 3, "totalTokenCount": 18}}}


def test_parser_handles_arbitrary_chunk_boundaries():
    raw = sse(TEXT1, TEXT2) + "data: [DONE]\n\n: comment\nevent: x\ndata: not-json\n"
    p = S.SSEParser()
    got = []
    for i in range(0, len(raw), 7):
        got += p.feed(raw[i:i + 7])
    got += p.flush()
    assert got == [TEXT1, TEXT2]


def test_parser_crlf_and_no_trailing_newline():
    p = S.SSEParser()
    assert p.feed('data: {"a":1}\r\n') == [{"a": 1}]
    assert p.feed('data: {"b":2}') == []
    assert p.flush() == [{"b": 2}]


def test_translate_text_usage_and_finish():
    evs = list(S.translate_stream([TEXT1, TEXT2]))
    assert [e.kind for e in evs] == ["text", "text", "finish"]
    fin = evs[-1]
    assert fin.finish_reason == "stop"
    assert fin.usage == {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18, "cached_tokens": 4, "reasoning_tokens": 3}


def test_translate_thinking_tool_call_and_signature():
    chunk = {"candidates": [{"content": {"parts": [
        {"text": "hmm", "thought": True, "thoughtSignature": "c2ln"},
        {"functionCall": {"name": "ls", "args": {"p": "/"}, "id": "weird id!"}, "thoughtSignature": "YWJj"},
    ]}, "finishReason": "STOP"}]}  # un-enveloped chunks also accepted
    evs = list(S.translate_stream([chunk]))
    assert evs[0].kind == "thinking" and evs[0].text == "hmm"
    tool = evs[1].tool
    assert tool == {"id": "weird_id_", "name": "ls", "arguments": '{"p": "/"}', "signature": "YWJj"}
    assert evs[-1].finish_reason == "tool_calls"  # STOP + tool call => tool_calls


def test_finish_reasons():
    assert S.map_stop_reason("MAX_TOKENS") == "length"
    assert S.map_stop_reason("SAFETY") == "content_filter"
    assert S.map_stop_reason("STOP") == "stop"
    assert S.map_stop_reason(None) == "stop"


def test_error_chunk_raises_redacted():
    with pytest.raises(S.AntigravityAPIError) as ei:
        list(S.translate_stream([{"error": {"message": "bad Bearer ya29.LEAK"}}]))
    assert "LEAK" not in str(ei.value)


@pytest.mark.parametrize("status,text,expect", [
    (429, '{"error":{"message":"Individual quota reached. Resets in 2h 3m."}}', "Quota reached. Please wait 2h 3m"),
    (429, '{"error":{"message":"Resource has been exhausted (e.g. check quota)."}}', "Rate limited"),
    (429, '{"error":{"message":"Quota exceeded for x"}}', "Quota reached."),
    (401, "x", "rejected the access token"),
    (404, '{"error":{"message":"Requested entity was not found."}}', "not available"),
    (503, '{"error":{"message":"No capacity available for model"}}', "no capacity"),
    (400, '{"error":{"message":"Unknown name \\"foo\\""}}', "format was rejected"),
    (418, '{"error":{"message":"teapot"}}', "teapot"),
])
def test_friendly_error(status, text, expect):
    assert expect.lower() in S.friendly_error(status, text).lower()


def test_hard_quota_detection():
    assert S.is_hard_quota_429("Resets in 1h")
    assert not S.is_hard_quota_429("rate limit exceeded")


# ── transport, with the network mocked ──────────────────────────────────────

def run(handler, model="gemini-3.8-flash-high", **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    p = S.StreamParams(model_id=model, messages=[{"role": "user", "content": "hi"}], token="tok", project_id="proj", **kw)
    return p, list(S.stream_generate(http, p))


def ok(req):
    return httpx.Response(200, text=sse(TEXT1, TEXT2), headers={"content-type": "text/event-stream"})


def test_stream_generate_happy_path_sends_expected_request():
    seen = {}

    def h(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return ok(req)

    p, evs = run(h)
    assert seen["url"] == "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
    assert seen["auth"] == "Bearer tok"
    b = seen["body"]
    assert b["project"] == "proj" and b["model"] == "gemini-3.8-flash-high" and b["requestType"] == "agent" and b["userAgent"] == "antigravity"
    assert b["request"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert "".join(e.text for e in evs if e.kind == "text") == "Hello"
    assert evs[-1].kind == "finish" and p.resolved_model == "gemini-3.8-flash-high"


def test_stream_falls_back_across_endpoints_on_503():
    urls = []

    def h(req):
        urls.append(req.url.host)
        return httpx.Response(503, text="x") if len(urls) == 1 else ok(req)

    p, evs = run(h)
    assert urls == ["daily-cloudcode-pa.googleapis.com", "daily-cloudcode-pa.sandbox.googleapis.com"]
    assert p.endpoint.endswith("sandbox.googleapis.com") and evs


def test_stream_404_falls_back_to_previous_generation_model():
    models = []

    def h(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(404, text='{"error":{"message":"Requested entity was not found."}}') if m.startswith("gemini-3.8") else ok(req)

    p, evs = run(h)
    assert p.resolved_model == "gemini-3.7-flash-high" and evs


def test_hard_quota_429_stops_immediately_with_friendly_message():
    n = []

    def h(req):
        n.append(1)
        return httpx.Response(429, text='{"error":{"message":"Individual quota reached. Resets in 1h."}}')

    with pytest.raises(S.AntigravityAPIError, match="Quota reached") as ei:
        run(h)
    assert len(n) == 1 and ei.value.status == 429


def test_non_retryable_400_does_not_try_other_endpoints():
    n = []
    with pytest.raises(S.AntigravityAPIError, match="Antigravity API error \\(400"):
        run(lambda r: (n.append(1), httpx.Response(400, text="bad"))[1])
    assert len(n) == 1


def test_empty_response_retries_then_errors(monkeypatch):
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    n = []
    with pytest.raises(S.AntigravityAPIError, match="empty response"):
        run(lambda r: (n.append(1), httpx.Response(200, text=""))[1])
    assert len(n) == 3


def test_runtime_model_env_override(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_RUNTIME_MODEL", "gemini-3.7-flash-low")
    seen = []
    run(lambda r: (seen.append(json.loads(r.content)["model"]), ok(r))[1])
    assert seen == ["gemini-3.7-flash-low"]
