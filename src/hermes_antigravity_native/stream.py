"""Native SSE transport (port of stream/stream.ts).

``SSEParser`` / ``ChunkTranslator`` are pure and unit-tested offline.
``stream_generate`` drives the real HTTP request with endpoint / model fallbacks.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import httpx

from . import constants as C
from . import models as M
from .client import antigravity_headers, endpoint_candidates, json_or_text_error
from .security import redact_secrets, safe_error
from .transform import build_request, sanitize_tool_call_id

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = (403, 404, 429, 500, 502, 503, 504)


class AntigravityAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# ── events ────────────────────────────────────────────────────────────────

@dataclass
class Event:
    kind: str  # text | thinking | tool_call | finish
    text: str = ""
    tool: dict[str, Any] | None = None
    finish_reason: str | None = None
    raw_finish_reason: str | None = None
    usage: dict[str, int] | None = None


def map_stop_reason(reason: str | None) -> str:
    """Gemini finishReason -> OpenAI finish_reason."""
    if reason == "STOP" or not reason:
        return "stop"
    if reason == "MAX_TOKENS":
        return "length"
    if reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
        return "content_filter"
    return "stop"


class SSEParser:
    """Incremental ``data:`` line parser tolerant of arbitrary chunk boundaries."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, data: str) -> list[dict[str, Any]]:
        self._buf += data
        out: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                break
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            parsed = self._line(line)
            if parsed is not None:
                out.append(parsed)
        return out

    def flush(self) -> list[dict[str, Any]]:
        line, self._buf = self._buf, ""
        parsed = self._line(line)
        return [parsed] if parsed is not None else []

    @staticmethod
    def _line(line: str) -> dict[str, Any] | None:
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            val = json.loads(payload)
        except ValueError:
            return None
        return val if isinstance(val, dict) else None


class ChunkTranslator:
    """Stateful: Cloud Code Assist stream chunk -> neutral ``Event``s."""

    def __init__(self) -> None:
        self.has_content = False
        self.saw_tool_call = False
        self.finish_reason: str | None = None
        self.raw_finish: str | None = None
        self.usage: dict[str, int] | None = None

    def feed(self, chunk: dict[str, Any]) -> list[Event]:
        err = chunk.get("error")
        if err:
            msg = err.get("message") if isinstance(err, dict) else None
            raise AntigravityAPIError(redact_secrets(msg or json.dumps(err)))
        data = chunk.get("response") if isinstance(chunk.get("response"), dict) else chunk
        cands = data.get("candidates") or []
        cand = cands[0] if cands and isinstance(cands[0], dict) else {}
        events: list[Event] = []
        for part in (cand.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("text") is not None and "functionCall" not in part:
                self.has_content = True
                events.append(Event("thinking" if part.get("thought") is True else "text", text=part["text"]))
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                self.has_content = True
                self.saw_tool_call = True
                events.append(Event("tool_call", tool={
                    "id": sanitize_tool_call_id(str(fc.get("id") or ""), fc.get("name")),
                    "name": fc.get("name") or "",
                    "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                    "signature": part.get("thoughtSignature"),
                }))
        if cand.get("finishReason"):
            self.raw_finish = cand["finishReason"]
            self.finish_reason = "tool_calls" if self.saw_tool_call else map_stop_reason(cand["finishReason"])
        um = data.get("usageMetadata")
        if isinstance(um, dict):
            prompt = int(um.get("promptTokenCount") or 0)
            cached = int(um.get("cachedContentTokenCount") or 0)
            thoughts = int(um.get("thoughtsTokenCount") or 0)
            completion = int(um.get("candidatesTokenCount") or 0) + thoughts
            self.usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": int(um.get("totalTokenCount") or (prompt + completion)),
                "cached_tokens": cached,
                "reasoning_tokens": thoughts,
            }
        return events

    def finish_event(self) -> Event:
        reason = self.finish_reason or ("tool_calls" if self.saw_tool_call else "stop")
        return Event("finish", finish_reason=reason, raw_finish_reason=self.raw_finish, usage=self.usage)


def translate_stream(chunks: Iterable[dict[str, Any]]) -> Iterator[Event]:
    tr = ChunkTranslator()
    for ch in chunks:
        yield from tr.feed(ch)
    yield tr.finish_event()


# ── errors ────────────────────────────────────────────────────────────────

def friendly_error(status: int | None, text: str) -> str:
    msg = redact_secrets(json_or_text_error(text))[:500]
    login = "run `hermes antigravity-native login`"
    if status == 400:
        if re.search(r"Requests ending with a model turn are not supported", msg, re.I):
            return "Antigravity rejected an invalid conversation message boundary. Next: add a user message / start a new session, then retry."
        if re.search(r"function call turn comes immediately after a user turn or after a function response turn", msg, re.I):
            return "Antigravity rejected an invalid function-call message boundary. Next: start a new session and retry; re-login is not required."
        if re.search(r"API key not valid|API_KEY_INVALID", msg, re.I):
            return f"Antigravity login expired or credentials are invalid. Next: {login}, then retry."
        if re.search(r"Invalid JSON payload|Unknown name", msg, re.I):
            return f"Antigravity request format was rejected by the backend ({msg}). Next: switch to a simpler model or retry."
        if re.search(r"Request contains an invalid argument", msg, re.I):
            return f"Antigravity rejected this request ({msg}). Next: retry once; if it keeps failing, switch models or re-login."
        return f"Bad request from Antigravity. Next: retry once, then {login} if it keeps failing. Backend said: {msg}"
    if status == 401:
        return f"Antigravity authentication failed. Next: {login}, then retry."
    if status == 403:
        if re.search(r"permission|forbidden|access", msg, re.I):
            return "Antigravity access was denied for this account or project. Next: try another model, re-login, or use an account with access."
        return f"Antigravity denied this request. Next: re-login or try another model. Backend said: {msg}"
    if status == 404:
        if re.search(r"Requested entity was not found", msg, re.I):
            return "This model is not available right now. Next: switch to gemini-3.8-flash-*, gemini-3.7-flash-*, or another working model."
        return f"Antigravity could not find the requested resource. Next: retry or switch models. Backend said: {msg}"
    if status == 408:
        return "Antigravity timed out. Next: retry the same request."
    if status == 409:
        return "Antigravity reported a conflict for this request. Next: retry once or start a new chat session."
    if status == 429:
        m = re.search(r"Resets? in ([^.\n]+)", msg, re.I)
        wait = m.group(1).strip() if m else None
        if re.search(r"Individual quota reached", msg, re.I):
            return f"Quota reached. Please wait {wait or 'for reset'}. Next: switch models or try again after reset."
        hard = bool(wait) or (not re.search(r"rate.?limit", msg, re.I) and re.search(r"quota exceeded|exceeded your|limit reached|reached your|daily limit", msg, re.I))
        if hard:
            return f"Quota reached.{f' Please wait {wait}.' if wait else ''} Next: switch models or retry later."
        return "Rate limited by Antigravity (429 ResourceExhausted). Next: retrying automatically; if it persists, switch models."
    if status == 500:
        return "Antigravity had an internal server error. Next: retry in a moment or switch models."
    if status == 502:
        return "Antigravity returned a bad gateway error. Next: retry in a moment."
    if status == 503:
        if re.search(r"No capacity available", msg, re.I):
            return "This model has no capacity right now. Next: retry later or switch to another model."
        return "Antigravity is temporarily unavailable. Next: retry in a moment or switch models."
    if status == 504:
        return "Antigravity timed out upstream. Next: retry in a moment."
    return msg


def is_hard_quota_429(text: str) -> bool:
    return bool(
        re.search(r"Individual quota reached", text, re.I)
        or re.search(r"Resets? in ", text, re.I)
        or (not re.search(r"rate.?limit", text, re.I) and re.search(r"quota exceeded|exceeded your|daily limit", text, re.I))
    )


def _env_seconds(name: str, default_s: float) -> float:
    raw = C.env(name)
    try:
        v = float(raw) / 1000 if raw else default_s
        return v if v > 0 else default_s
    except ValueError:
        return default_s


# ── transport ─────────────────────────────────────────────────────────────

@dataclass
class StreamParams:
    model_id: str
    messages: list[dict[str, Any]]
    token: str
    project_id: str
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None
    session_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    resolved_model: str | None = None
    endpoint: str | None = None


def stream_generate(http: httpx.Client, p: StreamParams) -> Iterator[Event]:
    """Drive ``:streamGenerateContent?alt=sse`` with endpoint/model fallbacks; yield Events."""
    base = C.env("RUNTIME_MODEL") or M.get_request_model_id(p.model_id, p.effort)
    candidates = [base]
    fb = M.get_fallback_runtime_model(base)
    if fb and fb != base:
        candidates.append(fb)
    timeout = httpx.Timeout(connect=10.0, read=_env_seconds("STREAM_HEADER_TIMEOUT_MS", 180.0), write=30.0, pool=10.0)
    headers = antigravity_headers(p.token)
    last_text, status, runtime_model = "", None, base

    for attempt in range(3):  # empty-response retries
        if attempt:
            time.sleep(0.5 * 2 ** (attempt - 1))
        resp: httpx.Response | None = None
        for ci, runtime_model in enumerate(candidates):
            p.resolved_model = runtime_model
            body, warns = build_request(
                model_id=p.model_id, runtime_model=runtime_model, project_id=p.project_id, messages=p.messages,
                tools=p.tools, tool_choice=p.tool_choice, temperature=p.temperature, max_tokens=p.max_tokens,
                effort=p.effort, session_id=p.session_id,
            )
            p.warnings = warns
            payload = json.dumps(body)
            resp = None
            for ep in endpoint_candidates():
                p.endpoint = ep
                try:
                    req = http.build_request("POST", f"{ep}/v1internal:streamGenerateContent?alt=sse", headers=headers, content=payload, timeout=timeout)
                    resp = http.send(req, stream=True)
                except httpx.HTTPError as exc:
                    last_text, status, resp = safe_error(exc), None, None
                    continue
                status = resp.status_code
                if resp.is_success:
                    break
                last_text = resp.read().decode("utf-8", "replace")
                resp.close()
                if status == 429 and is_hard_quota_429(last_text):
                    break
                if status not in RETRYABLE_STATUS:
                    break
            if resp is not None and resp.is_success:
                break
            if status == 404 and ci + 1 < len(candidates):
                continue
            break
        if resp is None or not resp.is_success:
            friendly = friendly_error(status, last_text)
            if status == 429 and "Quota reached." in friendly:
                raise AntigravityAPIError(friendly, status)
            raise AntigravityAPIError(
                f"Antigravity API error ({status or 'no response'}, endpoint={p.endpoint}, project={p.project_id}, runtimeModel={runtime_model}): {friendly or last_text}",
                status,
            )
        tr, parser, yielded = ChunkTranslator(), SSEParser(), False
        try:
            for text in resp.iter_text():
                for chunk in parser.feed(text):
                    for ev in tr.feed(chunk):
                        yielded = True
                        yield ev
            for chunk in parser.flush():
                for ev in tr.feed(chunk):
                    yielded = True
                    yield ev
        finally:
            resp.close()
        if tr.has_content or yielded:
            yield tr.finish_event()
            return
    raise AntigravityAPIError("Antigravity API returned an empty response")
