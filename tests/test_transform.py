import json

import pytest

from hermes_antigravity_native import schema as SC
from hermes_antigravity_native import transform as T

SIG = "YWJjZA=="  # valid base64, len%4==0


def conv(messages, model="claude-sonnet-4-6", runtime=None):
    return T.convert_messages(messages, model, runtime or model)


def test_system_and_user_basic():
    contents, system = conv([{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}])
    assert system == ["SYS"] and contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_default_system_instruction_when_none():
    body, _ = T.build_request(model_id="claude-sonnet-4-6", runtime_model="claude-sonnet-4-6", project_id="p", messages=[{"role": "user", "content": "x"}])
    texts = [p["text"] for p in body["request"]["systemInstruction"]["parts"]]
    assert texts[0].startswith("You are Antigravity")


def test_tool_roundtrip_claude_uses_ids_and_names():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call:1", "type": "function", "function": {"name": "ls", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "call:1", "content": "out"},
    ]
    contents, _ = conv(msgs)
    assert contents[1] == {"role": "model", "parts": [{"functionCall": {"name": "ls", "args": {"a": 1}, "id": "call_1"}}]}
    assert contents[2] == {"role": "user", "parts": [{"functionResponse": {"name": "ls", "response": {"output": "out"}, "id": "call_1"}}]}


def test_consecutive_tool_results_merge_into_one_user_turn():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "f", "arguments": "{}"}}, {"id": "b", "function": {"name": "g", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    contents, _ = conv(msgs)
    assert len(contents) == 3 and len(contents[2]["parts"]) == 2


def test_gemini3_unsigned_tool_call_becomes_observation_text():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "x", "function": {"name": "ls", "arguments": '{"p":"/"}'}}]},
        {"role": "tool", "tool_call_id": "x", "content": "files"},
    ]
    contents, _ = conv(msgs, "gemini-3.8-flash-high")
    flat = json.dumps(contents)
    assert "functionCall" not in flat and "functionResponse" not in flat
    assert "[Observation from `ls`" in flat and "files" in flat


def test_gemini3_signed_tool_call_is_kept_with_signature():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "x", "function": {"name": "ls", "arguments": "{}"}, "extra_content": {"google": {"thought_signature": SIG}}}]},
        {"role": "tool", "tool_call_id": "x", "content": "files"},
    ]
    contents, _ = conv(msgs, "gemini-3.8-flash-high")
    assert contents[1]["parts"] == [{"functionCall": {"name": "ls", "args": {}}, "thoughtSignature": SIG}]
    assert "id" not in contents[2]["parts"][0]["functionResponse"]  # gemini: no ids


def test_trailing_model_turn_gets_user_continuation():
    contents, _ = conv([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert contents[-1]["role"] == "user" and contents[-1]["parts"][0]["text"] == T.CONTINUATION_TEXT


def test_trailing_unanswered_tool_call_rejected():
    with pytest.raises(ValueError, match="missing tool result"):
        conv([{"role": "user", "content": "a"}, {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]}])


def test_function_call_turn_after_non_user_gets_bridge():
    msgs = [{"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "r"}]
    contents, _ = conv(msgs)
    assert contents[0] == {"role": "user", "parts": [{"text": T.CONTINUATION_TEXT}]}


def test_images_data_url():
    contents, _ = conv([{"role": "user", "content": [
        {"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}])
    assert contents[0]["parts"][1] == {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}


def test_sanitize_tool_call_id():
    assert T.sanitize_tool_call_id("a b/c") == "a_b_c"
    assert len(T.sanitize_tool_call_id("x" * 100)) == 64
    assert T.sanitize_tool_call_id("", "fn").startswith("fn_")


def test_signature_validation():
    assert T.is_valid_thought_signature(SIG)
    assert not T.is_valid_thought_signature("abc") and not T.is_valid_thought_signature("a!bc") and not T.is_valid_thought_signature("")


# ── tools / schema ────────────────────────────────────────────────────────

TOOL = {"type": "function", "function": {"name": "t", "description": "d", "parameters": {
    "$schema": "x", "type": "object", "properties": {"a": {"$ref": "#/$defs/A"}, "n": {"type": ["string", "null"], "format": "uri"}},
    "$defs": {"A": {"type": "integer", "description": "an int"}}}}}


def test_convert_tools_gemini_uses_json_schema_deref_and_strip():
    tools, warns = T.convert_tools([TOOL], legacy_parameters=False)
    d = tools[0]["functionDeclarations"][0]
    assert warns == [] and d["name"] == "t"
    s = d["parametersJsonSchema"]
    assert s["properties"]["a"] == {"type": "integer", "description": "an int"}
    assert "$schema" not in s and "$defs" not in s


def test_convert_tools_claude_uses_allowlisted_legacy_schema():
    tools, _ = T.convert_tools([TOOL], legacy_parameters=True)
    s = tools[0]["functionDeclarations"][0]["parameters"]
    assert s["properties"]["n"] == {"type": "string"}  # union collapsed, format dropped


def test_unresolvable_ref_skips_tool_with_warning():
    bad = {"type": "function", "function": {"name": "bad", "parameters": {"type": "object", "properties": {"x": {"$ref": "#/nope"}}}}}
    tools, warns = T.convert_tools([bad, TOOL], False)
    assert [d["name"] for d in tools[0]["functionDeclarations"]] == ["t"] and "bad" in warns[0]


def test_circular_ref_detected():
    s = {"type": "object", "properties": {"a": {"$ref": "#/$defs/N"}}, "$defs": {"N": {"type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}}}}
    with pytest.raises(SC.SchemaRefError):
        SC.dereference(s)


def test_ensure_root_object():
    assert SC.ensure_root_object(None) == {"type": "object", "properties": {}}
    assert SC.ensure_root_object({"properties": {"a": {}}})["type"] == "object"


# ── full request ──────────────────────────────────────────────────────────

def test_build_request_envelope_and_config():
    body, _ = T.build_request(
        model_id="gemini-3.8-flash", runtime_model="gemini-3.8-flash-high", project_id="proj",
        messages=[{"role": "user", "content": "hi"}], tools=[TOOL], tool_choice="required", temperature=0.2,
        max_tokens=10**9, effort="high")
    r = body["request"]
    assert body["model"] == "gemini-3.8-flash-high" and body["project"] == "proj"
    assert body["requestId"].startswith("agent/") and body["requestId"].endswith("/1")
    assert r["generationConfig"] == {"temperature": 0.2, "thinkingConfig": {"includeThoughts": True, "thinkingBudget": -1}, "maxOutputTokens": 65536}
    assert r["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert r["labels"]["model_enum"] == "MODEL_PLACEHOLDER_M318" and r["labels"]["used_claude"] == "false"
    assert r["sessionId"].isdigit()
    json.dumps(body)


def test_claude_labels_and_stable_trajectory():
    m = [{"role": "user", "content": "same"}]
    a, _ = T.build_request(model_id="claude-sonnet-4-6", runtime_model="claude-sonnet-4-6", project_id="p", messages=m)
    b, _ = T.build_request(model_id="claude-sonnet-4-6", runtime_model="claude-sonnet-4-6", project_id="p", messages=m)
    assert a["request"]["labels"]["trajectory_id"] == b["request"]["labels"]["trajectory_id"]
    assert a["request"]["labels"]["used_claude"] == "true" and a["request"]["labels"]["used_non_gemini_model"] == "true"


HERMES_SYS = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct.\n\n## Tools\nUse the terminal tool wisely."
)


def _system_text(body):
    return "\n".join(p["text"] for p in body["request"]["systemInstruction"]["parts"])


def test_hermes_identity_masked_by_default():
    msgs = [{"role": "system", "content": HERMES_SYS}, {"role": "developer", "content": "DEV"}, {"role": "user", "content": "hi"}]
    body, _ = T.build_request(model_id="gemini-3.8-flash-low", runtime_model="gemini-3.8-flash-low", project_id="p", messages=msgs)
    sys_text = _system_text(body)
    assert "Hermes Agent" not in sys_text and "Nous Research" not in sys_text
    assert sys_text.startswith(T.NEUTRAL_IDENTITY + " You are helpful, knowledgeable, and direct.")
    assert "## Tools\nUse the terminal tool wisely." in sys_text and "DEV" in sys_text
    assert msgs[0]["content"] == HERMES_SYS  # caller's messages are not mutated
    assert body["request"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_hermes_identity_mask_warns_loudly(caplog, monkeypatch):
    monkeypatch.setattr(T, "_mask_warned", False)
    with caplog.at_level("WARNING", logger="hermes_antigravity_native.transform"):
        T.mask_hermes_identity(HERMES_SYS)
    assert any("MASKING the Hermes Agent identity" in r.message for r in caplog.records)


@pytest.mark.parametrize("off", ["0", "false", "off", "no"])
def test_hermes_identity_left_intact_when_toggle_off(off, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_MASK_IDENTITY", off)
    assert not T.mask_identity_enabled()
    body, _ = T.build_request(model_id="gemini-3.8-flash-low", runtime_model="gemini-3.8-flash-low", project_id="p",
                              messages=[{"role": "system", "content": HERMES_SYS}, {"role": "user", "content": "hi"}])
    assert _system_text(body) == HERMES_SYS


def test_mask_only_touches_identity_sentence():
    assert T.mask_hermes_identity("Talk like Hermes Agent fans do.") == "Talk like Hermes Agent fans do."
    wrapped = "You are Hermes Agent, an intelligent AI assistant\ncreated by Nous Research. Rest."
    assert T.mask_hermes_identity(wrapped) == T.NEUTRAL_IDENTITY + " Rest."
