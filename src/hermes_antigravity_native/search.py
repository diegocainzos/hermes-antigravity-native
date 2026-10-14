"""Google Search Grounding via Antigravity / Cloud Code Assist.

Port of pi-antigravity's src/search/search.ts and src/features/google-search.ts.
Provides:
  1. execute_antigravity_search: Full grounded research with compact evidence brief,
     UTF-8 byte citations, and source links.
  2. AntigravityWebSearchProvider: Pluggable WebSearchProvider for Hermes's built-in web_search.
  3. google_search tool: Model-facing tool matching pi-antigravity's google_search.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from .auth import get_valid_credentials
from .client import (
    antigravity_headers,
    default_project_id,
    endpoint_candidates,
    json_or_text_error,
    native_extensions,
)
from .security import redact_secrets, safe_error

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_MODEL = "gemini-3.5-flash-lite"
SEARCH_MODEL_FALLBACKS: tuple[str, ...] = (
    DEFAULT_SEARCH_MODEL,
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
)

SEARCH_SYSTEM_INSTRUCTION = """You are a careful research assistant using Google Search grounding.
Return a compact evidence brief with exactly these sections:
## Findings
## Gaps
## Next checks

Rules:
- Verify each claim inside the specific item, function, or section it refers to. Do not copy an attribute from a neighboring entry on the same page.
- Prefer primary documentation and exact names, versions, and constraints over marketing summaries.
- If a fact is not supported by a retrieved source, put it under Gaps instead of stating it as a finding.
- Keep the brief short enough for an interactive agent tool call."""


@dataclass
class SearchSource:
    title: str
    url: str
    index: int | None = None


@dataclass
class SearchSupport:
    text: str
    source_indices: list[int]
    start_index: int | None = None
    end_index: int | None = None
    part_index: int | None = None


@dataclass
class SearchResult:
    text: str
    sources: list[SearchSource] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    supports: list[SearchSupport] = field(default_factory=list)


def parse_search_command_args(args: str) -> dict[str, Any]:
    tokens = [t for t in args.strip().split() if t]
    out: dict[str, Any] = {"query": ""}
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--thinking":
            out["thinking"] = True
            i += 1
            continue
        if token == "--url" and i + 1 < len(tokens):
            urls = out.setdefault("urls", [])
            urls.append(tokens[i + 1])
            i += 2
            continue
        rest.append(token)
        i += 1
    out["query"] = " ".join(rest)
    return out


def build_search_request(
    options: dict[str, Any],
    model: str,
    project_id: str,
) -> dict[str, Any]:
    instruction = (options.get("instruction") or "").strip()
    urls = options.get("urls") or []
    thinking = bool(options.get("thinking", False))
    query = (options.get("query") or "").strip()

    system_instruction_text = SEARCH_SYSTEM_INSTRUCTION
    if instruction:
        system_instruction_text += (
            f"\n\n[SPECIFIC DIRECTIVE FROM LEAD AGENT]:\n{instruction}\n"
            "You MUST strictly follow this directive during search query formulation and synthesis."
        )

    prompt = query
    if instruction:
        prompt = f"[Lead Agent Directive]: {instruction}\n\nSearch Query: {prompt}"
    if urls:
        prompt += f"\n\nURLs to analyze in detail:\n" + "\n".join(urls)

    tools: list[dict[str, Any]] = [{"googleSearch": {}}]
    if urls:
        tools.append({"urlContext": {}})

    thinking_budget = 4096 if thinking else 0
    req_id = f"search-{uuid.uuid4().hex[:12]}-{int(time.time())}"

    return {
        "project": project_id,
        "model": model,
        "request": {
            "systemInstruction": {
                "role": "user",
                "parts": [{"text": system_instruction_text}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "tools": tools,
            "generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": thinking_budget,
                    "includeThoughts": False,
                }
            },
        },
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": req_id,
    }


def parse_search_response(data: Any) -> SearchResult:
    result = SearchResult(text="")
    if not isinstance(data, dict):
        return result

    response_obj = data.get("response") if isinstance(data.get("response"), dict) else data
    candidates = response_obj.get("candidates") if isinstance(response_obj, dict) else []
    if not isinstance(candidates, list) or not candidates:
        err_obj = data.get("error") if isinstance(data.get("error"), dict) else None
        if not err_obj and isinstance(response_obj, dict):
            err_obj = response_obj.get("error") if isinstance(response_obj.get("error"), dict) else None
        msg = err_obj.get("message") if isinstance(err_obj, dict) else "No candidate returned from Antigravity Search"
        result.text = f"Error: {msg}"
        return result

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        result.text = "Error: Invalid candidate structure"
        return result

    # Extract text parts (excluding thinking parts)
    content = candidate.get("content")
    if isinstance(content, dict) and isinstance(content.get("parts"), list):
        for index, part in enumerate(content["parts"]):
            if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str) and part.get("text"):
                result.parts.append({"index": index, "text": part["text"]})
        result.text = "\n\n".join(p["text"] for p in result.parts)

    # Extract grounding metadata
    grounding = candidate.get("groundingMetadata")
    if isinstance(grounding, dict):
        queries = grounding.get("webSearchQueries")
        if isinstance(queries, list):
            result.queries = [q for q in queries if isinstance(q, str)]

        chunks = grounding.get("groundingChunks")
        if isinstance(chunks, list):
            for index, chunk in enumerate(chunks):
                if not isinstance(chunk, dict):
                    continue
                web = chunk.get("web")
                if isinstance(web, dict) and isinstance(web.get("uri"), str):
                    uri = web["uri"]
                    title = web.get("title")
                    title_str = title.strip() if isinstance(title, str) and title.strip() else uri
                    result.sources.append(SearchSource(index=index, title=title_str, url=uri))

        supports = grounding.get("groundingSupports")
        if isinstance(supports, list):
            source_indices = {s.index for s in result.sources if s.index is not None}
            text_part_indices = {p["index"] for p in result.parts}

            for support in supports:
                if not isinstance(support, dict):
                    continue
                segment = support.get("segment")
                if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                    continue
                raw_chunk_indices = support.get("groundingChunkIndices")
                if not isinstance(raw_chunk_indices, list):
                    continue

                part_index = segment.get("partIndex", 0)
                if not isinstance(part_index, int) or part_index not in text_part_indices:
                    continue

                # Filter valid integer chunk indices
                valid_indices = [
                    idx for idx in raw_chunk_indices
                    if isinstance(idx, int) and not isinstance(idx, bool) and idx in source_indices
                ]
                if not valid_indices:
                    continue

                start_idx = segment.get("startIndex")
                end_idx = segment.get("endIndex")
                result.supports.append(
                    SearchSupport(
                        text=segment["text"],
                        source_indices=valid_indices,
                        start_index=start_idx if isinstance(start_idx, int) else None,
                        end_index=end_idx if isinstance(end_idx, int) else None,
                        part_index=part_index,
                    )
                )

    return result


def cite_search_text(res: SearchResult) -> str:
    """Insert [1], [2] citation markers based on UTF-8 byte offsets."""
    if not res.supports:
        return res.text

    text_parts = res.parts or [{"index": 0, "text": res.text}]
    if "\n\n".join(p["text"] for p in text_parts) != res.text:
        return res.text

    source_indices = {s.index if s.index is not None else i for i, s in enumerate(res.sources)}
    cited_parts: list[str] = []

    for part in text_parts:
        part_idx = part.get("index", 0)
        part_text = part.get("text", "")
        raw_bytes = bytearray(part_text.encode("utf-8"))
        insertions: dict[int, list[int]] = {}

        for support in res.supports:
            if support.part_index != part_idx:
                continue
            start = support.start_index if support.start_index is not None else 0
            end = support.end_index
            if (
                not isinstance(start, int)
                or start < 0
                or end is None
                or not isinstance(end, int)
                or end <= start
                or end > len(raw_bytes)
                or raw_bytes[start:end].decode("utf-8", errors="replace") != support.text
            ):
                continue

            indices = [idx for idx in support.source_indices if idx in source_indices]
            if not indices:
                continue
            entry = insertions.setdefault(end, [])
            for idx in indices:
                if idx not in entry:
                    entry.append(idx)

        cited_b = raw_bytes
        for end_pos in sorted(insertions.keys(), reverse=True):
            refs = "".join(f"[{idx + 1}]" for idx in insertions[end_pos])
            ref_bytes = refs.encode("utf-8")
            cited_b = cited_b[:end_pos] + ref_bytes + cited_b[end_pos:]

        cited_parts.append(cited_b.decode("utf-8", errors="replace"))

    return "\n\n".join(cited_parts) or res.text


def format_search_result(res: SearchResult) -> str:
    sections: list[str] = []
    cited = cite_search_text(res)
    if cited:
        sections.append(cited)

    if res.sources:
        has_supports = bool(res.supports)
        list_items = []
        for index, s in enumerate(res.sources):
            prefix = f"[{s.index + 1 if s.index is not None else index + 1}] " if has_supports else ""
            list_items.append(f"- {prefix}[{s.title}]({s.url})")
        sections.append("### Sources\n" + "\n".join(list_items))

    if res.queries:
        q_str = ", ".join(f"`{q}`" for q in res.queries)
        sections.append(f"*Search queries: {q_str}*")

    return "\n\n".join(sections)


def execute_antigravity_search_raw(
    query: str,
    *,
    instruction: str | None = None,
    urls: Sequence[str] | None = None,
    thinking: bool = False,
    timeout: float = 30.0,
) -> SearchResult:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search query is required.")

    creds = get_valid_credentials()
    if not creds:
        raise RuntimeError("No Antigravity credentials found. Run `hermes antigravity-native login`.")

    project_id = creds.project_id or default_project_id(creds.email or "antigravity-default")
    headers = antigravity_headers(creds.access)
    exts = native_extensions()

    options = {
        "query": clean_query,
        "instruction": instruction,
        "urls": list(urls) if urls else None,
        "thinking": thinking,
    }

    last_error = "no endpoint available"

    for model in SEARCH_MODEL_FALLBACKS:
        req_payload = build_search_request(options, model, project_id)

        for endpoint in endpoint_candidates():
            url = f"{endpoint}/v1internal:generateContent"
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, headers=headers, json=req_payload, extensions=exts)

                if resp.status_code != 200:
                    last_error = json_or_text_error(resp.text)[:400]
                    if resp.status_code == 404 or resp.status_code in (429, 500, 502, 503, 504):
                        continue
                    raise RuntimeError(f"Antigravity search failed ({resp.status_code}): {last_error}")

                data = resp.json()
                cand_list = (
                    data.get("response", {}).get("candidates")
                    if isinstance(data.get("response"), dict)
                    else data.get("candidates")
                )
                first_cand = cand_list[0] if isinstance(cand_list, list) and cand_list else {}
                parts = first_cand.get("content", {}).get("parts", []) if isinstance(first_cand, dict) else []
                cand_text = parts[0].get("text") if parts and isinstance(parts[0], dict) else None

                if cand_text and "is no longer available. Please switch" in cand_text:
                    last_error = f"Model {model} deprecated: {cand_text[:100]}"
                    break

                return parse_search_response(data)

            except Exception as exc:
                if isinstance(exc, RuntimeError) and "Antigravity search failed" in str(exc):
                    raise
                last_error = safe_error(exc)[:400]
                continue

    raise RuntimeError(f"Antigravity Google Search failed: {last_error}")


def execute_antigravity_search(
    query: str,
    *,
    instruction: str | None = None,
    urls: Sequence[str] | None = None,
    thinking: bool = False,
    timeout: float = 30.0,
) -> str:
    res = execute_antigravity_search_raw(
        query=query,
        instruction=instruction,
        urls=urls,
        thinking=thinking,
        timeout=timeout,
    )
    return format_search_result(res)


# ---------------------------------------------------------------------------
# Hermes WebSearchProvider Implementation
# ---------------------------------------------------------------------------

try:
    from agent.web_search_provider import WebSearchProvider
except ImportError:
    # Standalone / mock fallback when testing outside full Hermes agent core
    class WebSearchProvider:  # type: ignore
        pass


class AntigravityWebSearchProvider(WebSearchProvider):
    """Hermes WebSearchProvider backed by Google Search Grounding via Antigravity."""

    def __init__(self, provider_name: str = "antigravity"):
        self._name = provider_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return "Google Search (Antigravity Grounding)"

    def is_available(self) -> bool:
        try:
            return bool(get_valid_credentials())
        except Exception:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute web search and return standard Hermes shape."""
        try:
            safe_limit = max(1, min(int(limit), 20))
            result = execute_antigravity_search_raw(query)

            if result.text.startswith("Error:"):
                return {"success": False, "error": result.text}

            hits = []
            for i, s in enumerate(result.sources[:safe_limit]):
                source_idx = s.index if s.index is not None else i
                # Look for matching snippet in grounded supports
                matching_texts = [
                    sup.text for sup in result.supports if source_idx in sup.source_indices
                ]
                snippet = " ".join(matching_texts) if matching_texts else (result.text[:250].strip() if result.text else "")
                hits.append({
                    "title": s.title,
                    "url": s.url,
                    "description": snippet,
                    "position": i + 1,
                })

            # If no sources returned but we have text, surface it as a single synthetic hit
            if not hits and result.text:
                hits.append({
                    "title": f"Google Search: {query}",
                    "url": "https://www.google.com",
                    "description": result.text[:500].strip(),
                    "position": 1,
                })

            return {
                "success": True,
                "data": {
                    "web": hits,
                },
            }
        except Exception as exc:
            logger.warning("AntigravityWebSearchProvider search failed: %s", exc)
            return {"success": False, "error": redact_secrets(str(exc))}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "free",
            "tag": "Google Search Grounding via Antigravity Cloud Code Assist.",
            "env_vars": [],
        }


# ---------------------------------------------------------------------------
# Tool registration helper
# ---------------------------------------------------------------------------

GOOGLE_SEARCH_SCHEMA = {
    "name": "google_search",
    "description": (
        "Search the web using Google Search via Antigravity Grounding. "
        "Returns a compact evidence brief with source citations. "
        "Use this whenever current, real-time information or web research is needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query or question.",
            },
            "instruction": {
                "type": "string",
                "description": (
                    "Specific directive or focus from the lead agent (e.g., "
                    "'focus on GitHub issues and benchmarks', 'ignore marketing announcements')."
                ),
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional specific URLs to fetch and analyze alongside the search.",
            },
            "thinking": {
                "type": "boolean",
                "description": "Enable deeper thinking for complex analysis (default: false).",
            },
        },
        "required": ["query"],
    },
}


def check_antigravity_search_available() -> bool:
    try:
        return bool(get_valid_credentials())
    except Exception:
        return False


def google_search_tool_handler(args: dict[str, Any], **kwargs: Any) -> str:
    query = args.get("query", "")
    instruction = args.get("instruction")
    urls = args.get("urls")
    thinking = bool(args.get("thinking", False))
    return execute_antigravity_search(
        query=query,
        instruction=instruction,
        urls=urls,
        thinking=thinking,
    )
