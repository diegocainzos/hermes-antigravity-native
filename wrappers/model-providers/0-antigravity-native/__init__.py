"""Thin file-plugin wrapper: delegates to the installed hermes_antigravity_native package."""

import sys

try:
    from hermes_antigravity_native.provider import register
except Exception as exc:  # fail loudly with an actionable message
    raise RuntimeError(
        "antigravity-native wrapper failed to load.\n"
        f"Python: {sys.executable}\n"
        f"Cause: {type(exc).__name__}: {exc}\n"
        "Fix: uv pip install --python /root/.hermes/hermes-agent/venv/bin/python -e /root/hermes-antigravity-provider"
    ) from exc

register()
