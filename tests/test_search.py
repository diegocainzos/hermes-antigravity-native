"""Unit tests for Antigravity Google Search grounding (port of pi-antigravity test-search.ts)."""

import pytest
from hermes_antigravity_native.search import (
    DEFAULT_SEARCH_MODEL,
    SEARCH_MODEL_FALLBACKS,
    SEARCH_SYSTEM_INSTRUCTION,
    AntigravityWebSearchProvider,
    SearchResult,
    SearchSource,
    SearchSupport,
    build_search_request,
    cite_search_text,
    format_search_result,
    parse_search_command_args,
    parse_search_response,
)


def test_parse_search_command_args():
    simple = parse_search_command_args("mimo 2.6 release date")
    assert simple["query"] == "mimo 2.6 release date"
    assert "thinking" not in simple
    assert "urls" not in simple

    with_flags = parse_search_command_args(
        "--thinking --url https://example.com/doc1 --url https://example.com/doc2 deep dive on specs"
    )
    assert with_flags["query"] == "deep dive on specs"
    assert with_flags["thinking"] is True
    assert with_flags["urls"] == ["https://example.com/doc1", "https://example.com/doc2"]


def test_build_search_request():
    req = build_search_request(
        {
            "query": "compare models",
            "instruction": "focus on benchmarks",
            "urls": ["https://benchmark.org"],
            "thinking": True,
        },
        DEFAULT_SEARCH_MODEL,
        "test-project-123",
    )

    assert req["project"] == "test-project-123"
    assert req["model"] == DEFAULT_SEARCH_MODEL
    assert DEFAULT_SEARCH_MODEL == "gemini-3.5-flash-lite"
    assert SEARCH_MODEL_FALLBACKS == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3-flash",
    )
    assert "## Findings" in SEARCH_SYSTEM_INSTRUCTION
    assert "## Gaps" in SEARCH_SYSTEM_INSTRUCTION
    assert "## Next checks" in SEARCH_SYSTEM_INSTRUCTION
    assert "neighboring entry" in SEARCH_SYSTEM_INSTRUCTION

    inner_req = req["request"]
    assert any("googleSearch" in t for t in inner_req["tools"])
    assert any("urlContext" in t for t in inner_req["tools"])
    assert "focus on benchmarks" in inner_req["contents"][0]["parts"][0]["text"]
    assert "focus on benchmarks" in inner_req["systemInstruction"]["parts"][0]["text"]
    assert inner_req["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 4096
    assert req["requestType"] == "agent"
    assert req["userAgent"] == "antigravity"
    assert isinstance(req["requestId"], str) and len(req["requestId"]) > 0


def test_parse_search_response():
    mock_api_response = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"thought": True, "text": "Searching internal thoughts..."},
                            {"text": "MiMo-V2.6 was released on September 22, 2026."},
                        ],
                    },
                    "finishReason": "STOP",
                    "groundingMetadata": {
                        "webSearchQueries": ["mimo 2.6 release", "mimo specs"],
                        "groundingChunks": [
                            {
                                "web": {
                                    "uri": "https://example.com/article",
                                    "title": "Example Article",
                                },
                            },
                        ],
                    },
                },
            ],
        },
    }

    parsed = parse_search_response(mock_api_response)
    assert parsed.text == "MiMo-V2.6 was released on September 22, 2026."
    assert parsed.queries == ["mimo 2.6 release", "mimo specs"]
    assert len(parsed.sources) == 1
    assert parsed.sources[0].url == "https://example.com/article"
    assert parsed.sources[0].title == "Example Article"

    markdown = format_search_result(parsed)
    assert "MiMo-V2.6 was released on September 22, 2026." in markdown
    assert "### Sources" in markdown
    assert "[Example Article](https://example.com/article)" in markdown
    assert "*Search queries: `mimo 2.6 release`, `mimo specs`*" in markdown


def test_utf8_byte_offsets_citations():
    text = "日本語 clock. More."
    first_end = len("日本語 clock.".encode("utf-8"))
    total_end = len(text.encode("utf-8"))

    grounded = parse_search_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "First part."},
                            {"thought": True, "text": "Hidden"},
                            {"text": text},
                        ]
                    },
                    "groundingMetadata": {
                        "groundingChunks": [
                            {"web": {"uri": "https://example.com/a", "title": "A"}},
                            {"other": {}},
                            {"web": {"uri": "https://example.com/b", "title": "B"}},
                        ],
                        "groundingSupports": [
                            {"segment": {"text": "First part.", "endIndex": 11}, "groundingChunkIndices": [0]},
                            {
                                "segment": {"text": "日本語 clock.", "endIndex": first_end, "partIndex": 2},
                                "groundingChunkIndices": [0, 2, 99, -1, 0.5, "0"],
                            },
                            {
                                "segment": {"text": "日本語 clock.", "endIndex": first_end, "partIndex": 2},
                                "groundingChunkIndices": [2, 0],
                            },
                            {
                                "segment": {"text": text, "endIndex": total_end, "partIndex": 2},
                                "groundingChunkIndices": [2],
                            },
                            {"segment": {"text": "Hidden", "endIndex": 6, "partIndex": 1}, "groundingChunkIndices": [0]},
                            {"segment": {"text": "Wrong", "endIndex": 5, "partIndex": 2}, "groundingChunkIndices": [0]},
                            {"segment": {"text": "Too far", "endIndex": 999, "partIndex": 2}, "groundingChunkIndices": [0]},
                            {"segment": {"text": "No offset"}, "groundingChunkIndices": [0]},
                            {"segment": {"text": "No web source", "endIndex": 13}, "groundingChunkIndices": [1]},
                            {"segment": None, "groundingChunkIndices": [0]},
                        ],
                    },
                }
            ],
        }
    )

    assert [p["index"] for p in grounded.parts] == [0, 2]
    assert [s.index for s in grounded.sources] == [0, 2]
    assert grounded.supports[1].source_indices == [0, 2]

    cited = format_search_result(grounded)
    assert cited.startswith("First part.[1]\n\n日本語 clock.[1][3] More.[3]")
    assert "- [3] [B](https://example.com/b)" in cited
    assert "Hidden" not in cited


def test_legacy_format_and_empty():
    legacy = SearchResult(
        text="Legacy result",
        sources=[SearchSource(title="Source", url="https://example.com")],
    )
    formatted = format_search_result(legacy)
    assert formatted == "Legacy result\n\n### Sources\n- [Source](https://example.com)"

    empty = SearchResult(text="No sources")
    assert format_search_result(empty) == "No sources"


def test_web_search_provider_class():
    prov = AntigravityWebSearchProvider()
    assert prov.name == "antigravity"
    assert prov.display_name == "Google Search (Antigravity Grounding)"
    assert prov.supports_search() is True
    assert prov.supports_extract() is False
