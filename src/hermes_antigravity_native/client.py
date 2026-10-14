"""Cloud Code Assist HTTP client (port of client/client.ts + utils/http.ts)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import httpx

from . import constants as C
from .models import is_usable_runtime_model_id, register_model_enums
from .security import assert_safe_api_base_url, redact_secrets, safe_error

logger = logging.getLogger(__name__)

_project_cache: dict[str, tuple[str | None, float]] = {}


def endpoint_candidates() -> list[str]:
    explicit = (C.env("BASE_URL") or "").strip()
    return [assert_safe_api_base_url(explicit)] if explicit else list(C.ENDPOINT_FALLBACKS)


def antigravity_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": C.env("USER_AGENT") or C.DEFAULT_USER_AGENT,
    }


def stable_project_id(seed: str) -> str:
    """UUIDv5-shaped stable id derived from a seed (account email)."""
    b = bytearray(hashlib.sha1(f"antigravity:{seed}".encode()).digest()[:16])
    b[6] = (b[6] & 0x0F) | 0x50
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def default_project_id(seed: str = "antigravity-default") -> str:
    return (C.env("PROJECT_ID") or "").strip() or stable_project_id(seed)


def _as_str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def extract_project_id(data: Any) -> str | None:
    """Pull a project id out of loadCodeAssist / listCloudAICompanionProjects payloads."""
    if not isinstance(data, dict):
        return None
    direct = next(
        (data[k] for k in (
            "antigravityProjectId", "projectId", "backendProjectId",
            "userDefinedCloudaicompanionProject", "cloudaicompanionProject", "project",
        ) if data.get(k)),
        None,
    )
    if _as_str(direct):
        return direct
    if isinstance(direct, dict) and _as_str(direct.get("id")):
        return direct["id"]
    for key in ("projects", "projectIds", "cloudaicompanionProjects"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                nested = extract_project_id(item)
                if nested:
                    return nested
                if _as_str(item):
                    return item
                if isinstance(item, dict) and _as_str(item.get("id")):
                    return item["id"]
    return None


def resolve_project_id(*, credential_project_id: str | None = None, warmed: str | None = None, email: str | None = None) -> str:
    return (C.env("PROJECT_ID") or "").strip() or warmed or credential_project_id or default_project_id(email or "antigravity-default")


def json_or_text_error(text: str) -> str:
    try:
        msg = json.loads(text).get("error", {}).get("message")
        if msg:
            return msg
    except (ValueError, AttributeError):
        pass
    return text


class AntigravityClient:
    """Thin sync client; one shared keep-alive httpx pool."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=C.DISCOVERY_TIMEOUT_S, write=30.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60.0),
        )

    def close(self) -> None:
        self.http.close()

    def post_json(self, endpoint: str, path: str, token: str, body: dict[str, Any], timeout: float = C.DISCOVERY_TIMEOUT_S) -> httpx.Response:
        return self.http.post(f"{endpoint}{path}", headers=antigravity_headers(token), content=json.dumps(body), timeout=timeout)

    def list_cloudaicompanion_projects(self, token: str) -> str | None:
        for ep in endpoint_candidates():
            try:
                r = self.post_json(ep, "/v1internal:listCloudAICompanionProjects", token, {})
                if r.status_code == 200:
                    return extract_project_id(r.json())
            except Exception as exc:
                logger.debug("listCloudAICompanionProjects %s: %s", ep, safe_error(exc))
        return None

    def _load_code_assist_uncached(self, token: str) -> str | None:
        for ep in endpoint_candidates():
            try:
                r = self.post_json(ep, "/v1internal:loadCodeAssist", token, {"metadata": {"ideType": "ANTIGRAVITY"}})
                if r.status_code != 200:
                    continue
                return extract_project_id(r.json()) or self.list_cloudaicompanion_projects(token)
            except Exception as exc:
                logger.debug("loadCodeAssist %s: %s", ep, safe_error(exc))
        return None

    def load_code_assist(self, token: str) -> str | None:
        """Project discovery with a 30-minute in-memory cache (keyed by token)."""
        hit = _project_cache.get(token)
        if hit and hit[1] > time.time():
            return hit[0]
        project = self._load_code_assist_uncached(token)
        _project_cache[token] = (project, time.time() + C.PROJECT_CACHE_TTL_S)
        while len(_project_cache) > 32:
            _project_cache.pop(next(iter(_project_cache)))
        return project

    def load_code_assist_raw(self, token: str) -> dict[str, Any] | None:
        for ep in endpoint_candidates():
            try:
                r = self.post_json(ep, "/v1internal:loadCodeAssist", token, {
                    "metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}})
                if r.status_code == 200:
                    return r.json()
            except Exception:
                continue
        return None

    def fetch_available_models(self, token: str, project_id: str) -> dict[str, Any]:
        """Merge fetchAvailableModels across endpoint candidates."""
        merged: dict[str, Any] = {}
        default_id: str | None = None
        last_err = "no endpoint available"
        ok = False
        for ep in endpoint_candidates():
            try:
                r = self.post_json(ep, "/v1internal:fetchAvailableModels", token, {"project": project_id})
                if r.status_code != 200:
                    last_err = f"{ep}: HTTP {r.status_code} {json_or_text_error(r.text)[:200]}"
                    continue
                ok = True
                data = r.json()
                if isinstance(data.get("models"), dict):
                    merged.update(data["models"])
                default_id = data.get("defaultAgentModelId") or data.get("defaultAgentModel") or default_id
            except Exception as exc:
                last_err = safe_error(exc)
        if not ok:
            raise RuntimeError(f"fetchAvailableModels failed: {redact_secrets(last_err)}")
        register_model_enums(merged)
        return {"models": merged, "defaultAgentModelId": default_id}

    def retrieve_user_quota_summary(self, token: str) -> dict[str, Any]:
        last = "no endpoint available"
        for ep in endpoint_candidates():
            try:
                r = self.post_json(ep, "/v1internal:retrieveUserQuotaSummary", token, {}, timeout=15.0)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {json_or_text_error(r.text)[:200]}"
            except Exception as exc:
                last = safe_error(exc)
        raise RuntimeError(redact_secrets(last))


def usable_model_ids(available: dict[str, Any]) -> list[str]:
    """Runtime ids from a fetchAvailableModels payload that Hermes can route to."""
    out = []
    for mid, info in (available.get("models") or {}).items():
        if isinstance(info, dict) and (info.get("isInternal") or str(mid).startswith(("chat_", "tab_"))):
            continue
        if is_usable_runtime_model_id(mid):
            out.append(mid)
    return sorted(out)
