"""Model catalog + routing (port of models/models.ts).

Public model ids -> backend ("runtime") request ids per thinking effort.
Ids that are already concrete runtime ids (``gemini-3.8-flash-high``) pass
through unchanged, so both ``gemini-3.8-flash`` + effort and the explicit
``agy models`` ids work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class Routing:
    routing: dict[str, str]
    default_request_id: str
    off: str | None = None


ROUTING: dict[str, Routing] = {
    "claude-opus-4-6": Routing(
        routing={k: "claude-opus-4-6-thinking" for k in ("minimal", "low", "medium", "high")},
        default_request_id="claude-opus-4-6-thinking",
    ),
    "claude-sonnet-4-6": Routing(
        off="claude-sonnet-4-6",
        routing={k: "claude-sonnet-4-6" for k in EFFORTS},
        default_request_id="claude-sonnet-4-6",
    ),
    "gemini-3.1-pro": Routing(
        off="gemini-3.1-pro-low",
        routing={
            "minimal": "gemini-3.1-pro-low",
            "low": "gemini-3.1-pro-low",
            "medium": "gemini-3.1-pro-low",
            "high": "gemini-pro-agent",
            "xhigh": "gemini-pro-agent",
        },
        default_request_id="gemini-3.1-pro-low",
    ),
    "gemini-3.5-flash": Routing(
        off="gemini-3.5-flash-extra-low",
        routing={
            "minimal": "gemini-3.5-flash-extra-low",
            "low": "gemini-3.5-flash-extra-low",
            "medium": "gemini-3.5-flash-low",
            "high": "gemini-3-flash-agent",
            "xhigh": "gemini-3-flash-agent",
        },
        default_request_id="gemini-3.5-flash-extra-low",
    ),
    "gpt-oss-120b": Routing(
        off="gpt-oss-120b-medium",
        routing={k: "gpt-oss-120b-medium" for k in ("minimal", "low", "medium", "high")},
        default_request_id="gpt-oss-120b-medium",
    ),
}
for _ver in ("3.8", "3.7", "3.6"):
    ROUTING[f"gemini-{_ver}-flash"] = Routing(
        off=f"gemini-{_ver}-flash-low",
        routing={
            "minimal": f"gemini-{_ver}-flash-low",
            "low": f"gemini-{_ver}-flash-low",
            "medium": f"gemini-{_ver}-flash-medium",
            "high": f"gemini-{_ver}-flash-high",
            "xhigh": f"gemini-{_ver}-flash-high",
        },
        default_request_id=f"gemini-{_ver}-flash-low",
    )

RUNTIME_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "claude-opus-4-6": 64000,
    "claude-opus-4-6-thinking": 64000,
    "claude-sonnet-4-6": 64000,
    "gpt-oss-120b": 32768,
    "gpt-oss-120b-medium": 32768,
    "gemini-3.1-pro": 65535,
    "gemini-3.1-pro-low": 65535,
    "gemini-3.1-pro-high": 65535,
    "gemini-pro-agent": 65535,
}

# Labels sent in request ``labels.model_enum`` (best effort; discovery can extend).
MODEL_ENUM: dict[str, str] = {
    "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
    "gemini-3.8-flash-low": "MODEL_PLACEHOLDER_M320",
    "gemini-3.7-flash-high": "MODEL_PLACEHOLDER_M298",
    "gemini-3.7-flash-medium": "MODEL_PLACEHOLDER_M299",
    "gemini-3.7-flash-low": "MODEL_PLACEHOLDER_M300",
    "gemini-3.6-flash-high": "MODEL_PLACEHOLDER_M71",
    "gemini-3.6-flash-medium": "MODEL_PLACEHOLDER_M72",
    "gemini-3.6-flash-low": "MODEL_PLACEHOLDER_M73",
    "gemini-3.5-flash-extra-low": "MODEL_PLACEHOLDER_M187",
    "gemini-3.5-flash-low": "MODEL_PLACEHOLDER_M20",
    "gemini-3-flash-agent": "MODEL_PLACEHOLDER_M84",
    "gemini-3.1-pro-low": "MODEL_PLACEHOLDER_M36",
    "gemini-3.1-pro-high": "MODEL_PLACEHOLDER_M37",
    "gemini-pro-agent": "MODEL_PLACEHOLDER_M16",
    "claude-sonnet-4-6": "MODEL_PLACEHOLDER_M35",
    "claude-opus-4-6-thinking": "MODEL_PLACEHOLDER_M26",
    "gpt-oss-120b-medium": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
}
_dynamic_enums: dict[str, str] = {}


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    context_window: int
    max_tokens: int
    reasoning: bool = True
    images: bool = True
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


# Mirrors `agy models`: public ids == runtime ids the CLI reports.
CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)", 1048576, 65536),
    ModelInfo("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)", 1048576, 65536),
    ModelInfo("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)", 1048576, 65536),
    ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", 200000, 64000),
    ModelInfo("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", 250000, 64000),
    ModelInfo("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", 131072, 32768, images=False),
)
CATALOG_BY_ID = {m.id: m for m in CATALOG}


def default_model_ids() -> list[str]:
    return [m.id for m in CATALOG]


def normalize_effort(effort: str | None) -> str | None:
    """Map Hermes/OpenAI effort strings to routing keys; ``None`` == off."""
    if effort is None:
        return None
    e = str(effort).strip().lower()
    if e in ("", "off", "none", "disabled", "false"):
        return None
    if e in ("max", "ultra"):
        return "xhigh"
    return e if e in EFFORTS else "medium"


def strip_provider_prefix(model_id: str) -> str:
    m = (model_id or "").strip()
    for prefix in ("antigravity-native/", "antigravity/"):
        if m.startswith(prefix):
            return m[len(prefix):]
    return m


def get_request_model_id(model_id: str, effort: str | None = None) -> str:
    """Resolve public model id + thinking effort to the backend request id."""
    model_id = strip_provider_prefix(model_id)
    r = ROUTING.get(model_id)
    if r is None:
        return model_id
    e = normalize_effort(effort)
    rt = r.routing
    if e is None:
        return r.off or rt.get("minimal") or rt.get("low") or r.default_request_id or model_id
    if e == "xhigh":
        return rt.get("xhigh") or rt.get("high") or rt.get("low") or rt.get("minimal") or r.off or r.default_request_id
    return rt.get(e) or rt.get("low") or rt.get("minimal") or r.off or r.default_request_id


def get_fallback_runtime_model(runtime_model: str) -> str | None:
    """Next-gen model not rolled out yet -> previous generation."""
    if runtime_model.startswith("gemini-3.8-flash-"):
        return runtime_model.replace("gemini-3.8-flash-", "gemini-3.7-flash-", 1)
    if runtime_model.startswith("gemini-3.7-flash-") and runtime_model != "gemini-3.7-flash-tiered":
        return runtime_model.replace("gemini-3.7-flash-", "gemini-3.6-flash-", 1)
    return None


def get_max_output_tokens(model_id: str, runtime_model: str | None = None) -> int:
    if runtime_model and runtime_model in RUNTIME_MAX_OUTPUT_TOKENS:
        return RUNTIME_MAX_OUTPUT_TOKENS[runtime_model]
    if model_id in RUNTIME_MAX_OUTPUT_TOKENS:
        return RUNTIME_MAX_OUTPUT_TOKENS[model_id]
    rt = runtime_model or model_id
    if rt.startswith("claude-"):
        return 64000
    if rt.startswith("gpt-oss-"):
        return 32768
    if rt.startswith("gemini-3.1-pro") or rt == "gemini-pro-agent":
        return 65535
    if rt.startswith("gemini-") or rt == "gemini-3-flash-agent":
        return 65536
    return 8192


def get_thinking_config(runtime_model: str, effort: str | None) -> dict[str, Any] | None:
    e = normalize_effort(effort)
    if runtime_model.startswith("claude-"):
        return {"includeThoughts": False, "thinkingBudget": 0} if e is None else {"includeThoughts": True, "thinkingBudget": 1024}
    if runtime_model.startswith("gpt-oss-"):
        return {"includeThoughts": False, "thinkingBudget": 0} if e is None else {"includeThoughts": True, "thinkingBudget": 8192}
    if runtime_model.startswith("gemini-3.5-flash") or runtime_model == "gemini-3-flash-agent":
        if e is None:
            return {"includeThoughts": False, "thinkingBudget": 0}
        return {"includeThoughts": True, "thinkingBudget": 10_000 if e in ("high", "xhigh") else 4_000 if e == "medium" else 1_000}
    if runtime_model.startswith("gemini-3.1-pro") or runtime_model == "gemini-pro-agent":
        if e is None:
            return {"includeThoughts": False, "thinkingBudget": 0}
        return {"includeThoughts": True, "thinkingBudget": 10_001 if e in ("high", "xhigh") else 1_001}
    if runtime_model.startswith("gemini-"):
        if e is None:
            return {"includeThoughts": False, "thinkingBudget": 0}
        return {"includeThoughts": True, "thinkingBudget": -1 if e in ("high", "xhigh") else 4_000 if e == "medium" else 1_000}
    return None


def register_model_enums(models: dict[str, Any]) -> None:
    """Learn ``model_enum`` labels from a fetchAvailableModels payload."""
    for wire_id, info in (models or {}).items():
        enum = info.get("model") if isinstance(info, dict) else None
        if isinstance(enum, str) and enum:
            _dynamic_enums[wire_id] = enum


def get_model_enum(wire_id: str) -> str | None:
    return _dynamic_enums.get(wire_id) or MODEL_ENUM.get(wire_id)


def is_usable_runtime_model_id(model_id: str) -> bool:
    import re

    return bool(re.match(r"^(gemini-|claude-|gpt-oss-)", model_id, re.I)) and not re.search(r"\s", model_id) and not re.match(r"^MODEL_", model_id, re.I)
