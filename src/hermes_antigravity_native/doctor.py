"""``doctor`` diagnostics (port of diagnostics/diagnostics.ts) — secrets always redacted."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Callable

from . import auth, constants as C, models as M, service
from .security import mask_email, safe_error


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | INFO | SKIP
    detail: str = ""

    def line(self) -> str:
        return f"[{self.status:<4}] {self.name}" + (f" — {self.detail}" if self.detail else "")


def hermes_registration() -> tuple[str, str]:
    """Is the provider visible to every Hermes catalog (`--provider`, `hermes model`, `/model`)?"""
    from providers import get_provider_profile
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.models import CANONICAL_PROVIDERS

    where = {
        "profile": get_provider_profile(C.PROVIDER_ID) is not None,
        "--provider": C.PROVIDER_ID in PROVIDER_REGISTRY,
        "picker": any(p.slug == C.PROVIDER_ID for p in CANONICAL_PROVIDERS),
    }
    missing = [k for k, ok in where.items() if not ok]
    if missing:
        return "FAIL", (f"missing from: {', '.join(missing)}. Re-run scripts/install.sh — the wrapper must live in "
                        "plugins/model-providers/0-antigravity-native so it loads before the legacy plugin.")
    return "PASS", "visible to `--provider`, `hermes model` and `/model`"


def legacy_plugin_state() -> tuple[str, str]:
    import httpx

    hooked = getattr(httpx.Client.send, "__name__", "") == "_global_send"
    loaded = any(m == "antigravity_auth" or m.startswith("antigravity_auth.") for m in sys.modules)
    if not (hooked or loaded):
        return "PASS", "legacy hermes-antigravity-auth not loaded"
    return "INFO", ("legacy hermes-antigravity-auth is loaded" + (" and hooks httpx.Client.send" if hooked else "")
                    + "; native requests carry its skip markers so it leaves them alone")


def run_doctor(*, live: bool = True, probe_model: str | None = "gemini-3.8-flash-low") -> list[Check]:
    checks: list[Check] = []

    def step(name: str, fn: Callable[[], tuple[str, str]]) -> None:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "FAIL", safe_error(exc)
        checks.append(Check(name, status, detail))

    box: dict = {}

    def c_creds() -> tuple[str, str]:
        creds = auth.load_credentials()
        if not creds:
            return "FAIL", "no credentials. Next: `hermes antigravity-native login` (or log in with agy once)"
        box["c"] = creds
        detail = (f"source={creds.source} account={mask_email(creds.email) or 'unknown'} "
                  f"oauth_client={C.client_project_number(creds.client_id or C.client_id())}")
        legacy = C.legacy_plugin_client()
        if creds.source == "hermes-antigravity-accounts" and legacy and creds.client_id == legacy[0]:
            return "WARN", (detail + " — this client's GCP project has the Cloud Code API disabled, so inference "
                            "will 403. Next: `hermes antigravity-native login`")
        return "PASS", detail

    step("credentials", c_creds)

    def c_store() -> tuple[str, str]:
        path = auth.native_store_path()
        data = auth._read_json(path)
        if data is None:
            return "INFO", f"own store absent ({path}); used only after `hermes antigravity-native login`"
        if auth.parse_native_store(data) is None:
            return "INFO", f"ignoring pre-v2 own store {path} (copied foreign token); safe to delete"
        return "PASS", f"own login store {path}"

    step("own store", c_store)
    step("catalog", lambda: ("PASS", f"{len(M.CATALOG)} models: " + ", ".join(M.default_model_ids())))
    step("hermes registration", hermes_registration)
    step("legacy plugin", legacy_plugin_state)

    from .gateway import gateway_api_key, gateway_base_url, gateway_key_path

    def c_gateway() -> tuple[str, str]:
        path = gateway_key_path()
        from_file = path.read_text().strip() if path.exists() else ""
        origin = "ANTIGRAVITY_NATIVE_API_KEY" if gateway_api_key() != from_file else str(path)
        return "INFO", f"loopback base_url={gateway_base_url()} key from {origin} (0600, never printed)"

    step("gateway", c_gateway)
    if not live:
        checks.append(Check("live checks", "SKIP", "--offline"))
        return checks
    if "c" not in box:
        return checks

    def c_refresh() -> tuple[str, str]:
        t = time.time()
        creds = auth.get_valid_credentials(force_refresh=True)
        box["live"] = creds
        return "PASS", (f"refreshed in-process via oauth_client={C.client_project_number(creds.client_id or C.client_id())} "
                        f"({int((creds.expires - time.time() * 1000) / 60000)} min left, {int((time.time() - t) * 1000)} ms)")

    step("token refresh", c_refresh)
    if "live" not in box:
        return checks

    def c_project() -> tuple[str, str]:
        _, project = service.resolve_session()
        box["project"] = project
        return "PASS", f"project={project}"

    step("project discovery", c_project)
    if "project" not in box:
        return checks

    def c_models() -> tuple[str, str]:
        from .client import usable_model_ids
        avail = service.shared_client().fetch_available_models(box["live"].access, box["project"])
        ids = usable_model_ids(avail)
        missing = [m for m in M.default_model_ids() if m not in ids]
        return ("PASS" if not missing else "WARN", f"{len(ids)} live models" + (f"; not offered by backend: {', '.join(missing)}" if missing else ""))

    step("model discovery", c_models)
    if not probe_model:
        return checks

    def c_chat() -> tuple[str, str]:
        from .stream import StreamParams, stream_generate
        p = StreamParams(model_id=probe_model, messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                         token=box["live"].access, project_id=box["project"], max_tokens=64)
        t = time.time()
        text = "".join(e.text for e in stream_generate(service.shared_client().http, p) if e.kind == "text")
        return "PASS", f"{probe_model} -> runtime={p.resolved_model} endpoint={p.endpoint} reply={text.strip()[:40]!r} ({int((time.time() - t) * 1000)} ms)"

    step(f"streaming chat ({probe_model})", c_chat)

    def c_gateway_chat() -> tuple[str, str]:
        """Same path Hermes uses: OpenAI-style SSE through the loopback gateway."""
        import httpx
        from .gateway import ensure_gateway_running

        base = ensure_gateway_running()
        text, t = "", time.time()
        with httpx.stream("POST", f"{base}/chat/completions", timeout=120, trust_env=False,
                          headers={"Authorization": f"Bearer {gateway_api_key()}"},
                          json={"model": probe_model, "stream": True, "max_tokens": 64,
                                "messages": [{"role": "user", "content": "Reply with the single word: pong"}]}) as r:
            if r.status_code != 200:
                r.read()
                return "FAIL", f"HTTP {r.status_code}: {safe_error(r.text)[:300]}"
            for line in r.iter_lines():
                if line.startswith("data: {"):
                    chunk = json.loads(line[6:])
                    if "error" in chunk:
                        return "FAIL", safe_error(chunk["error"].get("message"))[:300]
                    text += (chunk["choices"][0]["delta"].get("content") or "") if chunk.get("choices") else ""
        return "PASS", f"{base} SSE reply={text.strip()[:40]!r} ({int((time.time() - t) * 1000)} ms)"

    step("gateway chat (Hermes path)", c_gateway_chat)
    return checks
