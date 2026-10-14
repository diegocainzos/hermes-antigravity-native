"""OpenAI chat-completions request -> Cloud Code Assist ``streamGenerateContent`` body.

Port of pi-antigravity ``convertMessages`` / ``convertTools`` / ``buildRequest``,
adapted to the OpenAI message shape Hermes sends.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Any

from . import models as M
from .schema import SchemaRefError, dereference, ensure_root_object, normalize_custom_tool_schema, strip_meta

SYSTEM_INSTRUCTION = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by Google DeepMind. "
    "You are pair programming with a user to solve coding tasks. Be concise, practical, and tool-aware."
)
NO_PREAMBLE_INSTRUCTION = (
    'CRITICAL: NEVER output rule checks, formatting guidelines, constraint checklists (e.g. "No emdashes"), '
    "or your thinking/personality preambles in the final response. Output only the final response."
)
CONTINUATION_TEXT = "Continue the active task using the available instructions and context."

_SIG_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.S)
_counter = 0


def sanitize_tool_call_id(raw: str, fallback_name: str | None = None) -> str:
    global _counter
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw or "")[:64]
    if cleaned:
        return cleaned
    _counter += 1
    return f"{fallback_name or 'tool'}_{_counter}"


def tool_call_id_needed(model_id: str, runtime_model: str) -> bool:
    return any(x.startswith(("claude-", "gpt-oss-")) for x in (model_id, runtime_model))


def is_valid_thought_signature(sig: Any) -> bool:
    return isinstance(sig, str) and bool(sig) and len(sig) % 4 == 0 and bool(_SIG_RE.match(sig))


def gemini_requires_thought_signature(runtime_model: str) -> bool:
    if not runtime_model.startswith("gemini-"):
        return False
    m = re.match(r"^gemini-(\d+)", runtime_model)
    return int(m.group(1)) >= 3 if m else True


def sanitize_text(text: Any) -> str:
    # lone surrogates are not encodable as UTF-8 JSON
    return str("" if text is None else text).encode("utf-8", "replace").decode("utf-8", "replace")


def _content_parts(content: Any) -> list[dict[str, Any]]:
    """OpenAI message content -> Gemini text/inlineData parts."""
    if isinstance(content, str):
        return [{"text": sanitize_text(content)}] if content.strip() else []
    parts: list[dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t in ("text", "input_text") and str(item.get("text") or "").strip():
                parts.append({"text": sanitize_text(item["text"])})
            elif t in ("image_url", "input_image"):
                url = item.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                m = _DATA_URL_RE.match(url or "")
                if m:
                    parts.append({"inlineData": {"mimeType": m.group(1), "data": m.group(2).strip()}})
                elif url:
                    parts.append({"text": f"[image: {url}]"})
    return parts


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(i.get("text", "")) for i in content if isinstance(i, dict) and i.get("type") in ("text", "input_text"))
    return "" if content is None else str(content)


def append_turn(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": list(parts)})


def tool_call_signature(tc: dict[str, Any]) -> str | None:
    extra = tc.get("extra_content") or {}
    google = extra.get("google") if isinstance(extra, dict) else None
    if isinstance(google, dict):
        sig = google.get("thought_signature") or google.get("thoughtSignature")
        return sig if isinstance(sig, str) and sig else None
    return None


def convert_messages(messages: list[dict[str, Any]], model_id: str, runtime_model: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (contents, system_texts)."""
    contents: list[dict[str, Any]] = []
    system_texts: list[str] = []
    needs_sig = gemini_requires_thought_signature(runtime_model)
    id_needed = tool_call_id_needed(model_id, runtime_model)
    name_by_id: dict[str, str] = {}
    dropped: dict[str, str] = {}  # tool_call_id -> args text (unsigned gemini-3 calls rendered as text)

    for msg in messages:
        role = msg.get("role")
        if role in ("system", "developer"):
            txt = _text_of(msg.get("content")).strip()
            if txt:
                system_texts.append(sanitize_text(txt))
        elif role == "user":
            append_turn(contents, "user", _content_parts(msg.get("content")))
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            text = _text_of(msg.get("content"))
            if text.strip():
                parts.append({"text": sanitize_text(text)})
            tcs = [tc for tc in (msg.get("tool_calls") or []) if isinstance(tc, dict)]
            sigs = [tool_call_signature(tc) for tc in tcs]
            signed_group = bool(tcs) and is_valid_thought_signature(sigs[0]) and all(not s or is_valid_thought_signature(s) for s in sigs)
            for tc, sig in zip(tcs, sigs):
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                raw_id = str(tc.get("id") or "")
                name_by_id[raw_id] = name
                try:
                    args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
                except json.JSONDecodeError:
                    args = {"_raw": fn.get("arguments")}
                if not isinstance(args, dict):
                    args = {"_value": args}
                if needs_sig and not signed_group:
                    args_text = json.dumps(args, ensure_ascii=False)
                    dropped[raw_id] = args_text
                    dropped[sanitize_tool_call_id(raw_id, name)] = args_text
                    continue
                fc: dict[str, Any] = {"name": name, "args": args}
                if id_needed:
                    fc["id"] = sanitize_tool_call_id(raw_id, name)
                part: dict[str, Any] = {"functionCall": fc}
                if sig:
                    part["thoughtSignature"] = sig
                parts.append(part)
            append_turn(contents, "model", parts)
        elif role == "tool":
            raw_id = str(msg.get("tool_call_id") or "")
            name = name_by_id.get(raw_id) or str(msg.get("name") or "tool")
            text = _text_of(msg.get("content"))
            sanitized = sanitize_tool_call_id(raw_id, name) if id_needed else raw_id
            args_text = dropped.get(raw_id, dropped.get(sanitized)) if needs_sig else None
            if args_text is not None:
                label = f"`{name}`" if args_text == "{}" else f"`{name}` ({args_text})"
                append_turn(contents, "user", [{"text": sanitize_text(f"[Observation from {label}:\n{text}]")}])
            else:
                fr: dict[str, Any] = {"name": name, "response": {"output": sanitize_text(text)}}
                if id_needed:
                    fr["id"] = sanitized
                append_turn(contents, "user", [{"functionResponse": fr}])

    # A function-call model turn must directly follow a user turn.
    i = 0
    while i < len(contents):
        turn = contents[i]
        if turn["role"] == "model" and any("functionCall" in p for p in turn["parts"]) and (i == 0 or contents[i - 1]["role"] != "user"):
            contents.insert(i, {"role": "user", "parts": [{"text": CONTINUATION_TEXT}]})
            i += 1
        i += 1

    # Antigravity needs a natural-language user part somewhere.
    has_user_text = any(t["role"] == "user" and any(p.get("text", "").strip() for p in t["parts"] if "text" in p) for t in contents)
    if not has_user_text and contents:
        bridge = {"text": CONTINUATION_TEXT}
        first_user = next((t for t in contents if t["role"] == "user"), None)
        if first_user:
            first_user["parts"].append(bridge)
        else:
            contents.insert(0, {"role": "user", "parts": [bridge]})

    if contents and contents[-1]["role"] == "model":
        if any("functionCall" in p for p in contents[-1]["parts"]):
            raise ValueError("Antigravity request is missing tool result(s) for the final assistant tool call.")
        append_turn(contents, "user", [{"text": CONTINUATION_TEXT}])
    return contents, system_texts


def convert_tools(tools: list[dict[str, Any]] | None, legacy_parameters: bool) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """OpenAI ``tools`` -> Gemini ``functionDeclarations``; returns (tools, warnings)."""
    if not tools:
        return None, []
    decls: list[dict[str, Any]] = []
    warnings: list[str] = []
    for tool in tools:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        try:
            schema = dereference(fn.get("parameters"))
        except SchemaRefError as exc:
            warnings.append(f"Skipped tool '{fn['name']}': {exc}")
            continue
        schema = strip_meta(ensure_root_object(schema))
        decl = {"name": fn["name"], "description": fn.get("description") or ""}
        decl["parameters" if legacy_parameters else "parametersJsonSchema"] = normalize_custom_tool_schema(schema) if legacy_parameters else schema
        decls.append(decl)
    return ([{"functionDeclarations": decls}] if decls else None), warnings


def _tool_choice_mode(tool_choice: Any) -> str | None:
    if tool_choice in (None, "auto"):
        return None
    if tool_choice == "none":
        return "NONE"
    return "ANY"  # "required" or a specific function


def session_ids(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Stable conversation/trajectory ids for a chat (derived from its first message)."""
    first = next((m for m in messages if m.get("role") != "system"), messages[0] if messages else {})
    seed = f"{first.get('role', 'user')}:{_text_of(first.get('content'))[:64]}"
    return (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"antigravity:conv:{seed}")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"antigravity:traj:{seed}")),
    )


def build_request(
    *,
    model_id: str,
    runtime_model: str,
    project_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    effort: str | None = None,
    session_id: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    contents, system_texts = convert_messages(messages, model_id, runtime_model)
    system_parts = [{"text": t} for t in system_texts] or [{"text": SYSTEM_INSTRUCTION}, {"text": NO_PREAMBLE_INSTRUCTION}]
    if not any(t["role"] == "user" and any("text" in p and p["text"].strip() for p in t["parts"]) for t in contents) and system_texts:
        contents.insert(0, {"role": "user", "parts": [{"text": "Apply the active system instructions."}]})
    request: dict[str, Any] = {"contents": contents, "systemInstruction": {"role": "user", "parts": system_parts}}

    gen: dict[str, Any] = {}
    if temperature is not None:
        gen["temperature"] = temperature
    thinking = M.get_thinking_config(runtime_model, effort)
    if thinking:
        gen["thinkingConfig"] = thinking
    max_allowed = M.get_max_output_tokens(model_id, runtime_model)
    gen["maxOutputTokens"] = min(max_tokens, max_allowed) if max_tokens else max_allowed
    request["generationConfig"] = gen

    is_claude = model_id.startswith("claude-") or runtime_model.startswith("claude-")
    is_oss = model_id.startswith("gpt-oss-") or runtime_model.startswith("gpt-oss-")
    decls, warnings = convert_tools(tools, is_claude or is_oss)
    if decls:
        request["tools"] = decls
        mode = _tool_choice_mode(tool_choice)
        if mode:
            request["toolConfig"] = {"functionCallingConfig": {"mode": mode}}

    is_non_gemini = is_claude or is_oss or not (model_id.startswith("gemini-") or runtime_model.startswith("gemini-"))
    step = max(1, len(contents))
    request_index = sum(1 for m in messages if m.get("role") == "assistant")
    conversation_id, trajectory_id = session_ids(messages)
    labels = {
        "last_step_index": str(max(0, len(contents) - 1)),
        "request_id": f"{trajectory_id}-{request_index}",
        "trajectory_id": trajectory_id,
        "used_claude": "true" if is_claude else "false",
        "used_claude_conservative": "true" if is_claude else "false",
        "used_non_gemini_model": "true" if is_non_gemini else "false",
    }
    enum = M.get_model_enum(runtime_model)
    if enum:
        labels["model_enum"] = enum
    request["sessionId"] = session_id or str(random.getrandbits(63))
    request["labels"] = labels
    return {
        "project": project_id,
        "model": runtime_model,
        "request": request,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent/{conversation_id}/{int(time.time() * 1000)}/{trajectory_id}/{step}",
    }, warnings
