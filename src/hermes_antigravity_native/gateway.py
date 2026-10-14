"""In-process OpenAI-compatible gateway.

Hermes' ``ProviderProfile`` is declarative: core builds a plain OpenAI client
from ``base_url`` + ``api_key``. This module serves that base_url from a daemon
thread *inside the Hermes process* (127.0.0.1 only) and answers
``/v1/chat/completions`` by calling Cloud Code Assist directly over httpx —
no subprocess, no external CLI, no patching of Hermes.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx

from . import constants as C
from . import models as M
from . import service
from .auth import AuthError
from .security import redact_secrets
from .stream import AntigravityAPIError, Event, StreamParams, stream_generate

logger = logging.getLogger(__name__)
SERVER_ID = "hermes-antigravity-native"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def host_is_loopback(host_header: str) -> bool:
    """DNS-rebinding guard: only loopback ``Host`` headers are served."""
    h = host_header.strip().lower()
    if h.startswith("["):
        h = h.split("]")[0] + "]"
    else:
        h = h.rsplit(":", 1)[0] if ":" in h else h
    return h in _LOOPBACK_HOSTS


def gateway_port() -> int:
    try:
        return int(C.env("NATIVE_PORT") or C.DEFAULT_GATEWAY_PORT)
    except ValueError:
        return C.DEFAULT_GATEWAY_PORT


def gateway_base_url() -> str:
    return f"http://{C.DEFAULT_GATEWAY_HOST}:{gateway_port()}/v1"


def gateway_key_path() -> Path:
    return C.hermes_home() / "auth" / "antigravity_native_gateway.key"


def gateway_api_key() -> str:
    """Bearer the loopback gateway requires (Hermes sends it as the provider API key).

    ``ANTIGRAVITY_NATIVE_API_KEY`` if set, else a random per-user key kept 0600 in
    ``$HERMES_HOME/auth`` — so other local processes/users cannot spend this
    account's quota through the loopback port.
    """
    explicit = C.env("NATIVE_API_KEY")
    if explicit:
        return explicit
    path = gateway_key_path()
    try:
        key = path.read_text().strip()
        if key:
            return key
    except OSError:
        pass
    key = "agn-" + secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process won the race
        return path.read_text().strip()
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def bearer_matches(header: str | None, expected: str) -> bool:
    got = (header or "").strip()
    if got[:7].lower() == "bearer ":
        got = got[7:].strip()
    return bool(got) and hmac.compare_digest(got.encode(), expected.encode())


def extract_effort(body: dict[str, Any]) -> str | None:
    """Pull a thinking effort out of the many places OpenAI-compatible clients put it."""
    eff = body.get("reasoning_effort")
    if eff is None and isinstance(body.get("reasoning"), dict):
        r = body["reasoning"]
        eff = "none" if r.get("enabled") is False else r.get("effort")
    if eff is None and isinstance(body.get("extra_body"), dict):
        return extract_effort(body["extra_body"])
    return eff


def openai_error(status: int, message: str, code: str | None = None) -> dict[str, Any]:
    kind = {401: "authentication_error", 429: "rate_limit_error"}.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return {"error": {"message": redact_secrets(message), "type": kind, "code": code or str(status)}}


def _usage_obj(u: dict[str, int] | None) -> dict[str, Any] | None:
    if not u:
        return None
    return {
        "prompt_tokens": u["prompt_tokens"],
        "completion_tokens": u["completion_tokens"],
        "total_tokens": u["total_tokens"],
        "prompt_tokens_details": {"cached_tokens": u.get("cached_tokens", 0)},
        "completion_tokens_details": {"reasoning_tokens": u.get("reasoning_tokens", 0)},
    }


def _tool_call_obj(tool: dict[str, Any], index: int) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "index": index,
        "id": tool["id"] if tool["id"] and not tool["id"].startswith("tool_") else f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": tool["name"], "arguments": tool["arguments"]},
    }
    if tool.get("signature"):
        obj["extra_content"] = {"google": {"thought_signature": tool["signature"]}}
    return obj


def events_to_chunks(events: Iterator[Event], model: str) -> Iterator[dict[str, Any]]:
    """Neutral events -> OpenAI ``chat.completion.chunk`` dicts."""
    cid, created, n_tools = f"chatcmpl-{uuid.uuid4().hex[:16]}", int(time.time()), 0

    def chunk(delta: dict[str, Any], finish: str | None = None, usage: dict | None = None) -> dict[str, Any]:
        c: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            c["usage"] = usage
        return c

    yield chunk({"role": "assistant", "content": ""})
    for ev in events:
        if ev.kind == "text":
            yield chunk({"content": ev.text})
        elif ev.kind == "thinking":
            yield chunk({"reasoning_content": ev.text, "reasoning": ev.text})
        elif ev.kind == "tool_call" and ev.tool:
            yield chunk({"tool_calls": [_tool_call_obj(ev.tool, n_tools)]})
            n_tools += 1
        elif ev.kind == "finish":
            yield chunk({}, ev.finish_reason or "stop", _usage_obj(ev.usage))


def collect_response(chunks: Iterator[dict[str, Any]], model: str) -> dict[str, Any]:
    """Fold chunks into a non-streaming ``chat.completion``."""
    text, reasoning, tools, finish, usage, cid = [], [], [], "stop", None, None
    for c in chunks:
        cid = cid or c["id"]
        ch = c["choices"][0]
        d = ch["delta"]
        if d.get("content"):
            text.append(d["content"])
        if d.get("reasoning_content"):
            reasoning.append(d["reasoning_content"])
        for tc in d.get("tool_calls") or []:
            tools.append({k: v for k, v in tc.items() if k != "index"})
        finish = ch["finish_reason"] or finish
        usage = c.get("usage") or usage
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if reasoning:
        msg["reasoning_content"] = msg["reasoning"] = "".join(reasoning)
    if tools:
        msg["tool_calls"] = tools
    out = {"id": cid or f"chatcmpl-{uuid.uuid4().hex[:16]}", "object": "chat.completion", "created": int(time.time()),
           "model": model, "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


def run_chat(body: dict[str, Any], *, force_refresh: bool = False) -> tuple[StreamParams, Iterator[Event]]:
    """Resolve credentials/project and start the upstream stream (raises before any event)."""
    model = M.strip_provider_prefix(str(body.get("model") or ""))
    if not model:
        raise ValueError("`model` is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("`messages` must be a non-empty list")
    creds, project = service.resolve_session(force_refresh=force_refresh)
    params = StreamParams(
        model_id=model, messages=messages, token=creds.access, project_id=project,
        tools=body.get("tools"), tool_choice=body.get("tool_choice"), temperature=body.get("temperature"),
        max_tokens=body.get("max_completion_tokens") or body.get("max_tokens"), effort=extract_effort(body),
    )
    return params, stream_generate(service.shared_client().http, params)


def start_chat(body: dict[str, Any]) -> tuple[StreamParams, Iterator[Event], Event | None]:
    """``run_chat`` + pull the first event so upstream errors surface before any bytes are sent.

    An upstream 401 means the access token was revoked/expired early: refresh
    in-process once and retry; if that still fails, raise an actionable AuthError.
    """
    try:
        params, events = run_chat(body)
        return params, events, next(events, None)
    except AntigravityAPIError as exc:
        if exc.status != 401:
            raise
        logger.info("antigravity upstream 401; forcing a token refresh and retrying once")
    params, events = run_chat(body, force_refresh=True)
    try:
        return params, events, next(events, None)
    except AntigravityAPIError as exc:
        if exc.status != 401:
            raise
        raise AuthError(
            "Antigravity rejected a freshly refreshed access token (401). Next: run "
            "`hermes antigravity-native login`, then `hermes antigravity-native doctor`."
        ) from exc


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_ID

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("gateway: " + fmt, *args)

    # -- helpers
    def _host_ok(self) -> bool:
        return host_is_loopback(self.headers.get("Host") or "")

    def _json(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, obj: Any) -> None:
        data = b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    # -- routes
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._json(403, openai_error(403, "forbidden host"))
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", "/v1/health"):
            return self._json(200, {"status": "ok", "server": SERVER_ID})
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": m.id, "object": "model", "created": 0, "owned_by": "antigravity", "name": m.name,
                 "context_length": m.context_window} for m in M.CATALOG]})
        self._json(404, openai_error(404, "not found"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._json(403, openai_error(403, "forbidden host"))
        if self.path.split("?")[0].rstrip("/") != "/v1/chat/completions":
            return self._json(404, openai_error(404, "not found"))
        if not bearer_matches(self.headers.get("Authorization"), gateway_api_key()):
            return self._json(401, openai_error(401, "invalid antigravity-native gateway key (ANTIGRAVITY_NATIVE_API_KEY)", "invalid_api_key"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, openai_error(400, "invalid JSON body"))
        model = str(body.get("model") or "")
        try:
            params, events, first = start_chat(body)
        except AuthError as exc:
            return self._json(401, openai_error(401, str(exc), "auth_error"))
        except ValueError as exc:
            return self._json(400, openai_error(400, str(exc)))
        except AntigravityAPIError as exc:
            return self._json(exc.status or 502, openai_error(exc.status or 502, str(exc)))
        except httpx.HTTPError as exc:
            return self._json(502, openai_error(502, f"upstream connection error: {exc}"))
        except Exception as exc:  # pragma: no cover
            logger.exception("gateway internal error")
            return self._json(500, openai_error(500, f"internal error: {exc}"))

        def all_events() -> Iterator[Event]:
            if first is not None:
                yield first
            yield from events

        chunks = events_to_chunks(all_events(), model)
        if not body.get("stream"):
            try:
                return self._json(200, collect_response(chunks, model))
            except AntigravityAPIError as exc:
                return self._json(exc.status or 502, openai_error(exc.status or 502, str(exc)))
            except httpx.HTTPError as exc:
                return self._json(502, openai_error(502, f"upstream connection error: {exc}"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for c in chunks:
                self._sse(c)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (AntigravityAPIError, httpx.HTTPError) as exc:
            self._sse(openai_error(getattr(exc, "status", None) or 502, str(exc)))
        self._sse(b"[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_server: _Server | None = None
_lock = threading.Lock()


def _probe(port: int) -> bool:
    try:
        r = httpx.get(f"http://{C.DEFAULT_GATEWAY_HOST}:{port}/health", timeout=1.5, trust_env=False)
        return r.status_code == 200 and r.json().get("server") == SERVER_ID
    except Exception:
        return False


def ensure_gateway_running() -> str:
    """Start the loopback gateway once per process (reusing a live one on the port). Returns base_url."""
    global _server
    with _lock:
        if _server is not None:
            return gateway_base_url()
        port = gateway_port()
        try:
            srv = _Server((C.DEFAULT_GATEWAY_HOST, port), Handler)
        except OSError:
            if _probe(port):
                return gateway_base_url()
            raise RuntimeError(f"port {port} is in use by another service; set ANTIGRAVITY_NATIVE_PORT")
        threading.Thread(target=srv.serve_forever, name="antigravity-native-gateway", daemon=True).start()
        _server = srv
        return gateway_base_url()


def stop_gateway() -> None:
    global _server
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
