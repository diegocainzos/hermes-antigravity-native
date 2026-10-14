"""Coexistence with the legacy hermes-antigravity-auth plugin + Hermes provider selection."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from hermes_antigravity_native import client as K, doctor
from hermes_antigravity_native import stream as S

REPO = Path(__file__).resolve().parents[1]
WRAPPERS = REPO / "wrappers" / "model-providers"


def sse_ok():
    chunk = {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}}
    return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})


def test_stream_and_discovery_requests_carry_legacy_skip_markers():
    seen = []

    def h(req):
        seen.append({k: req.extensions.get(k) for k in K.LEGACY_INTERCEPTOR_SKIP})
        return sse_ok() if "streamGenerateContent" in str(req.url) else httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(h))
    list(S.stream_generate(http, S.StreamParams(model_id="gemini-3.8-flash-low", messages=[{"role": "user", "content": "hi"}], token="t", project_id="p")))
    K.AntigravityClient(http).post_json(K.C.DEFAULT_ENDPOINT, "/v1internal:loadCodeAssist", "t", {})
    assert seen == [dict.fromkeys(K.LEGACY_INTERCEPTOR_SKIP, True)] * 2


def test_native_extensions_are_fresh_per_request():
    a = K.native_extensions()
    a["mutated"] = True
    assert "mutated" not in K.native_extensions()


def test_survives_a_legacy_style_global_send_hook(monkeypatch):
    """Mimic the legacy interceptor: it strips Authorization from unmarked cloudcode-pa requests."""
    original = httpx.Client.send

    def _global_send(self, request, *a, **kw):
        if "cloudcode-pa" in str(request.url) and not request.extensions.get("antigravity_request_hook_processed"):
            del request.headers["authorization"]
        return original(self, request, *a, **kw)

    monkeypatch.setattr(httpx.Client, "send", _global_send)
    auths = []

    def h(req):
        auths.append(req.headers.get("authorization"))
        return sse_ok() if req.headers.get("authorization") else httpx.Response(401, text="no auth")

    http = httpx.Client(transport=httpx.MockTransport(h))
    evs = list(S.stream_generate(http, S.StreamParams(model_id="gemini-3.8-flash-low", messages=[{"role": "user", "content": "hi"}], token="tok", project_id="p")))
    assert auths == ["Bearer tok"] and "".join(e.text for e in evs if e.kind == "text") == "ok"
    status, detail = doctor.legacy_plugin_state()
    assert status == "INFO" and "skip markers" in detail


def test_doctor_reports_identity_masking(monkeypatch):
    assert doctor.identity_masking_state()[0] == "INFO" and "ON" in doctor.identity_masking_state()[1]
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_MASK_IDENTITY", "0")
    assert doctor.identity_masking_state()[0] == "WARN"


@pytest.mark.parametrize("mask", ["1", "0"])
def test_hermes_identity_429_message_reflects_masking(mask, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_MASK_IDENTITY", mask)
    msgs = [{"role": "system", "content": "You are Hermes Agent, an intelligent AI assistant ..."}, {"role": "user", "content": "hi"}]
    assert S.system_has_hermes_identity(msgs)
    assert not S.system_has_hermes_identity([{"role": "user", "content": "You are Hermes Agent"}])
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, text='{"error":{"code":429,"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}')))
    with pytest.raises(S.AntigravityAPIError) as ei:
        list(S.stream_generate(http, S.StreamParams(model_id="gemini-3.8-flash-low", messages=msgs, token="t", project_id="p")))
    assert ei.value.status == 429
    if mask == "1":
        assert "identity line was masked" in str(ei.value) and "not a quota problem" not in str(ei.value)
    else:
        assert "not a quota problem" in str(ei.value) and "MASK_IDENTITY=0" in str(ei.value)


# ── provider selection (`hermes -z --provider antigravity-native`) ──────────

def test_wrapper_dir_sorts_before_legacy_plugin():
    dirs = sorted(p.name for p in WRAPPERS.iterdir() if p.is_dir())
    assert dirs == ["0-antigravity-native"] and sorted(["antigravity", dirs[0]])[0] == dirs[0]


_PROBE = """
import json
import providers
providers.list_providers()            # discovery first, like `hermes -z`
from hermes_cli.auth import PROVIDER_REGISTRY, resolve_provider
from hermes_cli.models import CANONICAL_PROVIDERS
try:
    resolved = resolve_provider("antigravity-native")
except Exception as exc:
    resolved = "ERR " + str(exc)[:80]
print(json.dumps({"registry": "antigravity-native" in PROVIDER_REGISTRY,
                  "picker": any(p.slug == "antigravity-native" for p in CANONICAL_PROVIDERS),
                  "resolved": resolved}))
"""


def _hermes_core_available() -> bool:
    try:
        import providers  # noqa: F401
        import hermes_cli  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _hermes_core_available(), reason="needs Hermes core (run with Hermes' venv python)")
@pytest.mark.parametrize("dir_name,expect_ok", [("0-antigravity-native", True), ("antigravity-native", False)])
def test_selection_survives_legacy_plugin_import_order(tmp_path, dir_name, expect_ok):
    """The legacy plugin imports hermes_cli.auth mid-discovery, freezing PROVIDER_REGISTRY.

    Installed as ``0-antigravity-native`` we register first and stay selectable; the
    old ``antigravity-native`` dir name reproduces the original `Unknown provider` bug.
    """
    home = tmp_path / "home"
    mp = home / "plugins" / "model-providers"
    shutil.copytree(WRAPPERS / "0-antigravity-native", mp / dir_name)
    legacy = mp / "antigravity"
    legacy.mkdir()
    (legacy / "plugin.yaml").write_text("name: antigravity\nkind: model-provider\nversion: 0.0.0\n")
    (legacy / "__init__.py").write_text("import hermes_cli.auth  # what the legacy plugin effectively does\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTIGRAVITY_", "NOAGY_"))}
    env["HERMES_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    if expect_ok:
        assert res == {"registry": True, "picker": True, "resolved": "antigravity-native"}
    else:
        assert res["registry"] is False and res["resolved"].startswith("ERR")
