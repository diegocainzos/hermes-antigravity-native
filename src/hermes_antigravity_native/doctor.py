"""``doctor`` diagnostics (port of diagnostics/diagnostics.ts) — secrets always redacted."""

from __future__ import annotations

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


def run_doctor(*, live: bool = True, probe_model: str | None = "gemini-3.8-flash-low") -> list[Check]:
    checks: list[Check] = []

    def step(name: str, fn: Callable[[], tuple[str, str]]) -> None:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "FAIL", safe_error(exc)
        checks.append(Check(name, status, detail))

    creds_box: dict = {}

    def c_creds() -> tuple[str, str]:
        creds = auth.load_credentials()
        if not creds:
            return "FAIL", "no credentials (run `hermes antigravity-native login`)"
        creds_box["c"] = creds
        return "PASS", f"source={creds.source} account={mask_email(creds.email) or 'unknown'} stored_access_fresh={creds.is_fresh()}"

    step("credentials", c_creds)
    step("catalog", lambda: ("PASS", f"{len(M.CATALOG)} models: " + ", ".join(M.default_model_ids())))
    from .gateway import gateway_base_url
    step("gateway", lambda: ("INFO", f"loopback gateway base_url={gateway_base_url()}"))
    if not live:
        checks.append(Check("live checks", "SKIP", "--offline"))
        return checks
    if "c" not in creds_box:
        return checks

    def c_refresh() -> tuple[str, str]:
        t = time.time()
        creds = auth.get_valid_credentials()
        creds_box["live"] = creds
        return "PASS", f"access token valid ({int((creds.expires - time.time() * 1000) / 60000)} min left, {int((time.time() - t) * 1000)} ms)"

    step("token refresh", c_refresh)
    if "live" not in creds_box:
        return checks

    def c_project() -> tuple[str, str]:
        _, project = service.resolve_session()
        creds_box["project"] = project
        return "PASS", f"project={project}"

    step("project discovery", c_project)

    def c_models() -> tuple[str, str]:
        from .client import usable_model_ids
        avail = service.shared_client().fetch_available_models(creds_box["live"].access, creds_box["project"])
        ids = usable_model_ids(avail)
        missing = [m for m in M.default_model_ids() if m not in ids]
        return ("PASS" if not missing else "WARN", f"{len(ids)} live models" + (f"; not offered by backend: {', '.join(missing)}" if missing else ""))

    if "project" in creds_box:
        step("model discovery", c_models)

        if probe_model:
            def c_chat() -> tuple[str, str]:
                from .stream import StreamParams, stream_generate
                p = StreamParams(model_id=probe_model, messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                                 token=creds_box["live"].access, project_id=creds_box["project"], max_tokens=64)
                t = time.time()
                text = "".join(e.text for e in stream_generate(service.shared_client().http, p) if e.kind == "text")
                return "PASS", f"{probe_model} -> runtime={p.resolved_model} endpoint={p.endpoint} reply={text.strip()[:40]!r} ({int((time.time() - t) * 1000)} ms)"
            step(f"streaming chat ({probe_model})", c_chat)
    return checks
