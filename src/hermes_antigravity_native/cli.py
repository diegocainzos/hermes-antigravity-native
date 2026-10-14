"""``hermes antigravity-native <login|status|doctor|models|usage|refresh|serve>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import auth, constants as C, models as M
from .security import mask_email, safe_error


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="ag_cmd")
    sub.add_parser("login", help="Google OAuth login (paste-callback supported for headless machines)")
    sub.add_parser("status", help="Show credential source and gateway endpoint (no secrets)")
    d = sub.add_parser("doctor", help="Verify auth, project/model discovery and a streaming chat call")
    d.add_argument("--offline", action="store_true", help="skip network checks")
    d.add_argument("--no-chat", action="store_true", help="skip the live streaming chat probe")
    m = sub.add_parser("models", help="List models (add --live to query fetchAvailableModels + quota)")
    m.add_argument("--live", action="store_true")
    sub.add_parser("usage", help="Show shared quota pools (5h + weekly)")
    sub.add_parser("serve", help="Run the loopback OpenAI-compatible gateway in the foreground")
    parser.set_defaults(func=handle_cli)


def handle_cli(args: argparse.Namespace) -> int:
    cmd = getattr(args, "ag_cmd", None) or "status"
    try:
        return _COMMANDS[cmd](args)
    except Exception as exc:
        print(f"antigravity-native {cmd} failed: {safe_error(exc)}", file=sys.stderr)
        return 1


def _status(args) -> int:
    from .gateway import gateway_base_url
    c = auth.load_credentials()
    print(f"provider:    {C.PROVIDER_ID}\ngateway:     {gateway_base_url()}")
    print(f"credentials: {c.source + ' (' + (mask_email(c.email) or 'email unknown') + ')' if c else 'none — run `hermes antigravity-native login`'}")
    print(f"own store:   {auth.native_store_path()} ({'present' if auth.native_store_path().exists() else 'absent'})")
    return 0 if c else 1


def _login(args) -> int:
    creds = auth.login()
    print(f"Logged in as {mask_email(creds.email) or 'unknown account'}. Saved to {auth.native_store_path()} (0600).")
    return 0


def _doctor(args) -> int:
    from .doctor import run_doctor
    checks = run_doctor(live=not args.offline, probe_model=None if args.no_chat else "gemini-3.8-flash-low")
    for c in checks:
        print(c.line())
    return 1 if any(c.status == "FAIL" for c in checks) else 0


def _models(args) -> int:
    from . import service
    from .usage import parse_models, format_reset
    if not args.live:
        for m in M.CATALOG:
            print(f"{m.id:<28} {m.name}")
        return 0
    creds, project = service.resolve_session()
    data = service.shared_client().fetch_available_models(creds.access, project)
    print(f"project={project} default={data.get('defaultAgentModelId')}")
    for r in parse_models(data):
        rem = "?" if r["remaining"] is None else f"{r['remaining'] * 100:5.1f}%"
        print(f"{r['id']:<32} rem {rem:>6}  reset {format_reset(r['reset']):<8} {r['name'] or ''}")
    return 0


def _usage(args) -> int:
    from . import service
    from .usage import parse_quota_summary, format_quota
    creds, _ = service.resolve_session()
    try:
        print(format_quota(parse_quota_summary(service.shared_client().retrieve_user_quota_summary(creds.access))))
    except RuntimeError as exc:
        print(f"Aggregate quota summary unavailable: {safe_error(exc)[:200]}")
        return 1
    return 0


def _serve(args) -> int:
    import time
    from .gateway import ensure_gateway_running
    print(f"serving {ensure_gateway_running()} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


_COMMANDS = {"login": _login, "status": _status, "doctor": _doctor, "models": _models, "usage": _usage, "serve": _serve}
