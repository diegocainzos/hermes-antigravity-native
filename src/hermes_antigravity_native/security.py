"""Secret redaction and URL safety helpers (port of utils/security.ts)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_PATTERNS = [
    (re.compile(r"\bya29\.[A-Za-z0-9._~+/-]+=*"), "[redacted-access-token]"),
    (re.compile(r"\b1//?[A-Za-z0-9_-]{20,}"), "[redacted-refresh-token]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I), "Bearer [redacted]"),
    (
        re.compile(
            r'("?(?:access_token|refresh_token|id_token|token|client_secret|code_verifier|authorization|access|refresh)"?\s*[:=]\s*")[^"]*(")',
            re.I,
        ),
        r"\1[redacted]\2",
    ),
    (
        re.compile(
            r'("?(?:access_token|refresh_token|id_token|token|client_secret|code_verifier|authorization)"?\s*[:=]\s*)[^\s&,}"]+',
            re.I,
        ),
        r"\1[redacted]",
    ),
]


def redact_secrets(text: object) -> str:
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def mask_email(email: str | None) -> str | None:
    if not email or not isinstance(email, str):
        return None
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return "[redacted-email]"
    name, domain = parts
    masked = f"{name[0]}***{name[-1]}" if len(name) > 2 else f"{name[0]}***"
    return f"{masked}@{domain}"


def safe_error(exc: object) -> str:
    return redact_secrets(str(exc))


def resolve_callback_host(raw: str | None = None) -> str:
    host = (raw or "127.0.0.1").strip().lower()
    if host not in _LOOPBACK:
        raise ValueError(
            f'Unsafe callback host "{host}". Only loopback hosts are allowed: 127.0.0.1, ::1, localhost.'
        )
    return "127.0.0.1" if host == "localhost" else host


def assert_safe_api_base_url(raw: str) -> str:
    """Refuse non-https / non-googleapis base URLs so tokens cannot be exfiltrated."""
    try:
        url = urlparse(raw)
        host = (url.hostname or "").lower()
    except ValueError as exc:
        raise ValueError(f"Invalid ANTIGRAVITY_BASE_URL: {raw}") from exc
    if url.scheme != "https":
        raise ValueError(f"ANTIGRAVITY_BASE_URL must use https (got {url.scheme or 'none'})")
    if url.username or url.password:
        raise ValueError("ANTIGRAVITY_BASE_URL must not include credentials")
    if not (host == "googleapis.com" or host.endswith(".googleapis.com")):
        raise ValueError(f'ANTIGRAVITY_BASE_URL host "{host}" is not allowed. Use a *.googleapis.com endpoint.')
    path = url.path.rstrip("/")
    return f"{url.scheme}://{url.netloc}{path}"
