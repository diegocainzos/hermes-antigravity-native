"""Quota pools / model listing (port of usage/usage.ts, condensed)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _clamp(v: Any) -> float | None:
    return max(0.0, min(1.0, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bar(rem: float | None, width: int = 20) -> str:
    if rem is None:
        return "[" + "?" * width + "]"
    n = round(rem * width)
    return "[" + "#" * n + "-" * (width - n) + "]"


def format_reset(reset: str | None, now: datetime | None = None) -> str:
    if not reset:
        return "n/a"
    try:
        ts = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return reset
    mins = round((ts - (now or datetime.now(timezone.utc))).total_seconds() / 60)
    if mins <= 0:
        return "now"
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def parse_quota_summary(data: Any) -> list[dict[str, Any]]:
    groups = []
    for g in (data or {}).get("groups") or []:
        buckets = []
        for b in g.get("buckets") or []:
            rem = _clamp(b.get("remainingFraction"))
            if rem is None and not b.get("bucketId"):
                continue
            buckets.append({"name": str(b.get("displayName") or b.get("bucketId") or "Limit"), "remaining": rem or 0.0,
                            "reset": b.get("resetTime")})
        groups.append({"name": str(g.get("displayName") or "Quota group"), "buckets": buckets})
    return groups


def format_quota(groups: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for g in groups:
        if lines:
            lines.append("")
        lines.append(g["name"])
        for b in g["buckets"]:
            lines.append(f"  {_bar(b['remaining'])} {b['name']}: {round(b['remaining'] * 1000) / 10}% left · resets {format_reset(b['reset'])}")
    return "\n".join(lines) or "No quota groups returned."


def parse_models(data: Any) -> list[dict[str, Any]]:
    rows = []
    for mid, info in ((data or {}).get("models") or {}).items():
        if not isinstance(info, dict) or info.get("isInternal") or str(mid).startswith("chat_"):
            continue
        qi = info.get("quotaInfo") if isinstance(info.get("quotaInfo"), dict) else {}
        rows.append({"id": mid, "name": info.get("displayName") or info.get("label"),
                     "remaining": _clamp(qi.get("remainingFraction")), "reset": qi.get("resetTime")})
    return sorted(rows, key=lambda r: r["id"])
