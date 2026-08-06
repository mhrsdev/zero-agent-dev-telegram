"""Zero v2 CLI — Click-based command-line interface."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import click

from zero import __version__

__all__ = ["main"]


@click.group()
@click.version_option(__version__, prog_name="zero")
def main() -> None:
    """Zero v2 — Telegram-based AI collaboration platform."""


@main.command()
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
def init(config_path: str | None) -> None:
    """Initialize Zero configuration."""
    from pathlib import Path  # noqa: PLC0415

    from zero.core.config import DEFAULT_CONFIG_PATHS  # noqa: PLC0415

    target = Path(config_path) if config_path else DEFAULT_CONFIG_PATHS[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        click.echo(f"Config already exists at {target}")
        return
    target.write_text(
        "# Zero v2 config\n"
        "# See docs/adr/0007-config-and-secrets.md for full schema\n\n"
        "database:\n"
        "  backend: sqlite\n"
        "  sqlite_dir: ~/.zero/db\n\n"
        "telegram:\n"
        "  bot_token: secret://env/TELEGRAM_BOT_TOKEN\n\n"
        "router:\n"
        "  base_url: http://127.0.0.1:8080/v1\n"
        "  api_key: secret://env/ZERO_ROUTER_API_KEY\n\n"
        "logging:\n"
        "  level: info\n"
        "  format: json\n",
        encoding="utf-8",
    )
    click.echo(f"Created config at {target}")
    click.echo("Next steps:")
    click.echo("  1. Set TELEGRAM_BOT_TOKEN env var")
    click.echo("  2. Set ZERO_ROUTER_API_KEY env var")
    click.echo("  3. Run: zero serve")


@main.command()
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value (writes to ~/.zero/config.yaml)."""

    import yaml  # noqa: PLC0415

    from zero.core.config import DEFAULT_CONFIG_PATHS  # noqa: PLC0415

    path = DEFAULT_CONFIG_PATHS[0]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Walk dotted key.
    parts = key.split(".")
    cursor = data
    for p in parts[:-1]:
        if p not in cursor or not isinstance(cursor[p], dict):
            cursor[p] = {}
        cursor = cursor[p]
    cursor[parts[-1]] = value
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    click.echo(f"Set {key} = {value!r} in {path}")


@main.command()
def doctor() -> None:
    """Run health checks."""
    from zero.core.config import get_config  # noqa: PLC0415
    from zero.core.secret import CompositeSecretResolver  # noqa: PLC0415

    click.echo(f"Zero v2 {__version__}")
    try:
        cfg = get_config()
        click.echo("  config loaded: OK")
        click.echo(f"  database backend: {cfg.database.backend}")
    except Exception as e:
        click.echo(f"  config: FAILED — {e}")
        return

    # Check secrets exist.
    resolver = CompositeSecretResolver()
    if cfg.telegram is not None:
        if resolver.exists(cfg.telegram.bot_token):
            click.echo("  telegram token: configured")
        else:
            click.echo(f"  telegram token: NOT FOUND ({cfg.telegram.bot_token})")
    if cfg.router is not None:
        if resolver.exists(cfg.router.api_key):
            click.echo("  router key: configured")
        else:
            click.echo(f"  router key: NOT FOUND ({cfg.router.api_key})")


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["polling", "webhook"]),
    default="polling",
    help="Bot run mode (default: polling)",
)
@click.option(
    "--drop-pending",
    is_flag=True,
    default=False,
    help="Drop pending Telegram updates on startup",
)
@click.option(
    "--provider",
    type=click.Choice(["gemini", "openai", "openrouter", "custom", "shim"]),
    default=None,
    help="Override the LLM provider (default: from config)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Set up the runner without starting the bot (for testing)",
)
def serve(mode: str, drop_pending: bool, provider: str | None, dry_run: bool) -> None:
    """Start the Telegram bot — REAL agent loop, not a stub.

    Wires together:
        TelegramBot → AgentLoop → RouterClient → LLM provider (Gemini/OpenAI/...)

    The LLM provider is selected via ``router.provider`` in config or the
    ``--provider`` flag. For ``gemini``/``openai``/``openrouter``, a local
    RouterShim is started that proxies to the real LLM API.

    Set the following env vars before running:
        TELEGRAM_BOT_TOKEN — Telegram bot token from @BotFather
        ZERO_ROUTER_PROVIDER — gemini | openai | openrouter | custom
        GEMINI_API_KEY (if provider=gemini)
        OPENAI_API_KEY (if provider=openai)
        OPENROUTER_API_KEY (if provider=openrouter)
        ZERO_ROUTER_API_KEY (if provider=custom; for external Router service)
    """
    from zero.agents.runner import ZeroAgentRunner, ZeroAgentRunnerConfig  # noqa: PLC0415
    from zero.core.config import get_config, reset_config_cache  # noqa: PLC0415

    # If --provider is given, set the env var before config loads.
    if provider is not None:
        import os  # noqa: PLC0415

        os.environ["ZERO_ROUTER__PROVIDER"] = provider
        reset_config_cache()

    cfg = get_config()

    if cfg.telegram is None:
        click.echo("Error: telegram config is missing. Run 'zero init' first.", err=True)
        return

    if cfg.router is None:
        click.echo(
            "Error: router config is missing. Set ZERO_ROUTER__API_KEY or run 'zero init'.",
            err=True,
        )
        return

    click.echo(f"Starting Zero Agent v{__version__} (provider={cfg.router.provider})...")

    runner = ZeroAgentRunner(ZeroAgentRunnerConfig(dry_run=dry_run))

    try:
        asyncio.run(_run_serve(runner, mode, dry_run))
    except KeyboardInterrupt:
        click.echo("Interrupted by user.")
        asyncio.run(runner.stop())


async def _run_serve(runner: Any, mode: str, dry_run: bool) -> None:
    """Async entrypoint for the serve command."""
    await runner.setup()
    if dry_run:
        # Just print the setup summary and exit.
        click.echo("Dry run — setup complete. Not starting the bot.")
        provider = runner.provider
        click.echo(f"  Provider: {provider.provider_name if provider else '(none)'}")
        shim = runner.shim
        click.echo(f"  RouterShim: {shim.base_url if shim else '(external)'}")
        click.echo(f"  Database: {runner.db}")
        await runner.stop()
        return
    if mode == "webhook":
        # Webhook mode not yet fully wired in runner — fall back to polling
        # for now (webhook is rarely used in dev).
        click.echo("Note: webhook mode not yet supported via runner; using polling.")
    await runner.start()
    await runner.stop()


@main.group()
def mcp() -> None:
    """MCP (Model Context Protocol) commands."""


@mcp.command("serve")
@click.option(
    "--name",
    default="zero-v2",
    help="Server name reported to MCP clients",
)
def mcp_serve(name: str) -> None:
    """Run Zero as an MCP server (stdio JSON-RPC).

    Configure in Claude Desktop or other MCP clients:
    {
      "mcpServers": {
        "zero": {
          "command": "zero",
          "args": ["mcp", "serve"]
        }
      }
    }
    """
    from zero.mcp.server import McpServer, McpServerTool, serve_stdio  # noqa: PLC0415

    server = McpServer(server_name=name, version=__version__)

    # Register a basic echo tool so the server is useful out-of-the-box.
    async def echo_handler(args, ctx):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        return f"Echo: {args.get('text', '')}"

    server.register_tool(McpServerTool(
        name="echo",
        description="Echo back the input text (test tool)",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo_handler,
    ))

    click.echo(f"Starting MCP server {name!r} on stdio...", err=True)
    asyncio.run(serve_stdio(server))


if __name__ == "__main__":
    main()
