"""Standalone Hermes plugin entry: registers ``hermes antigravity-native``."""

from .cli import handle_cli, setup_cli


def register(ctx) -> None:
    ctx.register_cli_command(
        name="antigravity-native",
        help="Native Antigravity provider: login / doctor / models / usage",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Manage the antigravity-native Hermes model provider",
    )
