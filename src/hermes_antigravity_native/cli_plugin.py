"""Standalone Hermes plugin entry: registers ``hermes antigravity-native`` and search tools."""

from __future__ import annotations

import logging

from .cli import handle_cli, setup_cli
from .search import (
    GOOGLE_SEARCH_SCHEMA,
    AntigravityWebSearchProvider,
    check_antigravity_search_available,
    google_search_tool_handler,
)

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="antigravity-native",
        help="Native Antigravity provider: login / doctor / models / usage",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Manage the antigravity-native Hermes model provider",
    )

    # 1. Register WebSearchProvider so Hermes built-in `web_search` tool can route to Antigravity
    try:
        ctx.register_web_search_provider(AntigravityWebSearchProvider("antigravity"))
        ctx.register_web_search_provider(AntigravityWebSearchProvider("google"))
        logger.debug("Registered Antigravity WebSearchProvider (antigravity, google)")
    except Exception as exc:
        logger.debug("Could not register Antigravity WebSearchProvider: %s", exc)

    # 2. Register explicit `google_search` tool matching pi-antigravity's interface
    try:
        ctx.register_tool(
            name="google_search",
            toolset="web",
            schema=GOOGLE_SEARCH_SCHEMA,
            handler=google_search_tool_handler,
            check_fn=check_antigravity_search_available,
            description=GOOGLE_SEARCH_SCHEMA["description"],
            emoji="🔍",
        )
        logger.debug("Registered google_search tool in 'web' toolset")
    except Exception as exc:
        logger.debug("Could not register google_search tool: %s", exc)
