import pytest

from hermes_antigravity_native import models as M


@pytest.mark.parametrize("pub,effort,expected", [
    ("gemini-3.8-flash", None, "gemini-3.8-flash-low"),
    ("gemini-3.8-flash", "off", "gemini-3.8-flash-low"),
    ("gemini-3.8-flash", "low", "gemini-3.8-flash-low"),
    ("gemini-3.8-flash", "medium", "gemini-3.8-flash-medium"),
    ("gemini-3.8-flash", "high", "gemini-3.8-flash-high"),
    ("gemini-3.8-flash", "xhigh", "gemini-3.8-flash-high"),
    ("gemini-3.8-flash", "max", "gemini-3.8-flash-high"),
    ("gemini-3.1-pro", "high", "gemini-pro-agent"),
    ("gemini-3.5-flash", "medium", "gemini-3.5-flash-low"),
    ("claude-opus-4-6", "high", "claude-opus-4-6-thinking"),
    ("claude-sonnet-4-6", None, "claude-sonnet-4-6"),
    ("gpt-oss-120b", "high", "gpt-oss-120b-medium"),
    # concrete runtime ids pass through, effort ignored
    ("gemini-3.8-flash-high", "low", "gemini-3.8-flash-high"),
    ("claude-opus-4-6-thinking", None, "claude-opus-4-6-thinking"),
    ("antigravity-native/gemini-3.8-flash-medium", None, "gemini-3.8-flash-medium"),
])
def test_routing(pub, effort, expected):
    assert M.get_request_model_id(pub, effort) == expected


def test_catalog_matches_agy_models():
    assert M.default_model_ids() == [
        "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
    ]


def test_fallback_chain():
    assert M.get_fallback_runtime_model("gemini-3.8-flash-high") == "gemini-3.7-flash-high"
    assert M.get_fallback_runtime_model("gemini-3.7-flash-low") == "gemini-3.6-flash-low"
    assert M.get_fallback_runtime_model("claude-sonnet-4-6") is None


def test_thinking_and_max_tokens():
    assert M.get_thinking_config("claude-sonnet-4-6", None) == {"includeThoughts": False, "thinkingBudget": 0}
    assert M.get_thinking_config("claude-sonnet-4-6", "high")["thinkingBudget"] == 1024
    assert M.get_thinking_config("gemini-3.8-flash-high", "high")["thinkingBudget"] == -1
    assert M.get_thinking_config("gemini-3.8-flash-low", "low")["thinkingBudget"] == 1000
    assert M.get_max_output_tokens("claude-sonnet-4-6") == 64000
    assert M.get_max_output_tokens("x", "gpt-oss-120b-medium") == 32768
    assert M.get_max_output_tokens("gemini-3.8-flash-high") == 65536
    assert M.get_max_output_tokens("mystery") == 8192


def test_model_enum_dynamic_overrides_static():
    assert M.get_model_enum("gemini-3.8-flash-low") == "MODEL_PLACEHOLDER_M320"
    M.register_model_enums({"gemini-3.8-flash-low": {"model": "MODEL_X"}})
    assert M.get_model_enum("gemini-3.8-flash-low") == "MODEL_X"


def test_usable_runtime_id():
    assert M.is_usable_runtime_model_id("gemini-3.8-flash-low")
    assert not M.is_usable_runtime_model_id("MODEL_PLACEHOLDER_M1")
    assert not M.is_usable_runtime_model_id("chat_23310")
    assert not M.is_usable_runtime_model_id("gemini with space")
