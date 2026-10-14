"""Thin file-plugin wrapper for the antigravity-native CLI commands."""

import sys

try:
    from hermes_antigravity_native.cli_plugin import register
except Exception as exc:
    raise RuntimeError(
        "antigravity-native-cli wrapper failed to load.\n"
        f"Python: {sys.executable}\nCause: {type(exc).__name__}: {exc}\n"
        "Fix: uv pip install --python /root/.hermes/hermes-agent/venv/bin/python -e /root/hermes-antigravity-provider"
    ) from exc
