"""Minimal Textual overview screen (M8 first slice)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar


def run() -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Footer, Header, Static
    except ImportError:
        print(
            "TUI requires the [tui] extra:\n"
            "  pip install 'zero-develop[tui]'\n"
            "(or: pip install textual)"
        )
        return 2

    from zero.manage.core.config import ConfigService

    home = Path(os.environ.get("ZERO_HOME", Path.home() / ".zero"))
    cfgsvc = ConfigService(home)
    cfg = cfgsvc.load() if cfgsvc.exists() else None

    class ZeroApp(App):
        BINDINGS: ClassVar[list] = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]
        TITLE = "Zero Dev Telegram"

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static(self._overview(), id="overview")
            yield Footer()

        def action_refresh(self) -> None:
            self.query_one("#overview", Static).update(self._overview())

        def _overview(self) -> str:
            if cfg is None:
                return "[warn] config not initialized — run: zero setup"
            lines = [
                f"environment : {cfg.server.environment}",
                (
                    f"telegram    : mode={cfg.telegram.mode} "
                    f"bot={cfg.telegram.bot_username or '-'} "
                    f"token={'yes' if cfg.telegram.bot_token_ref else 'no'}"
                ),
                f"access      : {cfg.access.mode} groups={len(cfg.access.groups)}",
            ]
            for p in cfg.providers:
                lines.append(
                    f"provider    : {p.id} ({p.protocol}) models={','.join(p.models) or '-'}"
                )
            r = cfg.routing
            lines.append(
                f"routing     : primary={r.primary_model or '-'} "
                f"fallbacks={','.join(r.fallback_models) or '-'}"
            )
            return "\n".join(lines)

    ZeroApp().run()
    return 0
