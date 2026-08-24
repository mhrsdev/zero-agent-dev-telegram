"""Zero Dev Telegram full TUI (Textual).

Screens: Overview / Telegram / Groups / Providers / Usage / System /
Backups / Diagnostics. All payloads come from the pure data layer in
``zero.manage.tui.data``; the app is presentation only. Keyboard-first:
1-9 jump sections, r refreshes, q quits.
"""

from __future__ import annotations

from typing import ClassVar


def run() -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import VerticalScroll
        from textual.widgets import DataTable, Footer, Header, Static
    except ImportError:
        print(
            "TUI requires the [tui] extra:\n"
            "  pip install 'zero-develop[tui]'\n"
            "(or: pip install textual)"
        )
        return 2

    from zero.manage.tui import data

    class _Panel(Static):
        """Base panel: pulls a fresh payload from the data layer."""

        source = "overview"  # data.<source>() name

        def on_mount(self) -> None:
            self.refresh_data()

        def refresh_data(self) -> None:
            payload = getattr(data, self.source)()
            self.update(self._render(payload))

        def _render(self, payload):
            return str(payload)

    class OverviewPanel(_Panel):
        BORDER_TITLE = "Overview"

        def _render(self, o):
            lines = [
                f"environment : {o['environment']}",
                f"config      : {o['config_path']}",
                (
                    f"telegram    : mode={o['telegram']['mode']} "
                    f"bot={o['telegram']['bot']} token={o['telegram']['token']}"
                ),
                f"access      : {o['access']['mode']} groups={o['access']['groups']}",
                (
                    "routing     : primary="
                    f"{o['routing'].get('primary', '-')} fallbacks="
                    + ",".join(o["routing"].get("fallbacks", []))
                ),
                "",
                "providers:",
            ]
            if not o["providers"]:
                lines.append("  (none configured — open Wizard)")
            for p in o["providers"]:
                models = ",".join(p["models"]) or "-"
                lines.append(
                    f"  {p['id']:<22} {p['protocol']:<20} models={models} prio={p['priority']}"
                )
            b = o["backups"]
            lines += [
                "",
                f"backups     : schedule={b['schedule']} archives={len(b['archives'])}",
            ]
            return "\n".join(lines)

    class TelegramPanel(_Panel):
        source = "telegram_screen"
        BORDER_TITLE = "Telegram"

        def _render(self, t):
            tg = t["telegram"]
            head = (
                f"mode={tg.get('mode', '-')} bot={tg.get('bot', '-')} "
                f"token={'configured' if tg.get('token') == 'yes' else 'not set'}\n"
            )
            events = "\n".join(t.get("events", [])) or "(no recent events)"
            return head + events

    class GroupsPanel(_Panel):
        source = "groups_screen"
        BORDER_TITLE = "Groups"

        def _render(self, rows):
            if not rows:
                return "no groups configured\n(add via Wizard or `zero telegram groups add`)"
            header = f"{'chat id':<20}{'title':<26}{'state':<6}{'limits'}"
            lines = [header]
            for g in rows:
                lines.append(
                    f"{g.get('chat_id', ''):<20}"
                    f"{(g.get('title') or '-'):<26}"
                    f"{'on' if g.get('enabled') else 'off':<6}"
                    f"{g.get('rate_limit_per_min')}/min "
                    f"{g.get('daily_token_budget'):,} tok/day"
                )
            return "\n".join(lines)

    class ProvidersScreen(DataTable):
        BORDER_TITLE = "Providers"

        def on_mount(self) -> None:
            self.add_columns(
                "id", "protocol", "enabled", "models", "prio", "tool_calls", "streaming"
            )
            for r in data.providers_screen():
                self.add_row(
                    r["id"],
                    r["protocol"],
                    str(r["enabled"]),
                    str(r["models"]),
                    str(r["priority"]),
                    r["tool_calls"],
                    r["streaming"],
                )

    class UsagePanel(_Panel):
        source = "usage_screen"
        BORDER_TITLE = "Usage (estimates)"

        def _render(self, rows):
            if not rows:
                return "no usage recorded yet"
            lines = [
                (
                    f"{'day':<12}{'provider':<20}{'model':<24}"
                    f"{'req':>5}{'in':>9}{'out':>9}{'cost$':>10}"
                )
            ]
            for r in rows[:40]:
                lines.append(
                    f"{r['day']:<12}{r['provider']:<20}{r['model']:<24}"
                    f"{r['requests']:>5}{r['input_tokens']:>9}"
                    f"{r['output_tokens']:>9}{r['cost']:>10}"
                )
            return "\n".join(lines)

    class SystemPanel(_Panel):
        source = "system_screen"
        BORDER_TITLE = "System"

        def _render(self, s):
            return (
                f"python      : {s['python']}\n"
                f"disk free   : {s['disk_free_gb']} GB\n"
                f"service     : {s['service_kind']}\n"
                f"config home : {s['config_home']}"
            )

    class BackupsPanel(_Panel):
        source = "backups_screen"
        BORDER_TITLE = "Backups"

        def _render(self, b):
            lines = [
                f"schedule: {b['schedule']}  archives: {len(b['archives'])}",
            ]
            last = b.get("last") or {}
            if last.get("path"):
                lines.append(f"last    : {last['path']}")
            for a in b["archives"][:15]:
                lines.append(f"  {a['name']}  {a['size']:,}B  {a['age_h']}h")
            return "\n".join(lines)

    class DiagnosticsPanel(_Panel):
        source = "diagnostics_screen"
        BORDER_TITLE = "Diagnostics (doctor)"

        def _render(self, report):
            sym = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
            body = "\n".join(
                f"{sym[c['status']]} {c['name']}: {c['detail']}" for c in report["checks"]
            )
            s = report["summary"]
            return f"{body}\n\n{s['total']} checks · {s['fail']} fail · {s['warn']} warn"

    PANELS = {
        "overview": OverviewPanel,
        "telegram": TelegramPanel,
        "groups": GroupsPanel,
        "providers": ProvidersScreen,
        "usage": UsagePanel,
        "system": SystemPanel,
        "backups": BackupsPanel,
        "diag": DiagnosticsPanel,
    }

    class ZeroTUI(App):
        TITLE = "Zero Dev Telegram"
        BINDINGS: ClassVar[list] = [
            ("1", "show_panel('overview')", "Overview"),
            ("2", "show_panel('telegram')", "Telegram"),
            ("3", "show_panel('groups')", "Groups"),
            ("4", "show_panel('providers')", "Providers"),
            ("5", "show_panel('usage')", "Usage"),
            ("6", "show_panel('system')", "System"),
            ("7", "show_panel('backups')", "Backups"),
            ("8", "show_panel('diag')", "Doctor"),
            ("r", "refresh_panel", "Refresh"),
            ("q", "quit", "Quit"),
        ]

        current: ClassVar[str] = "overview"

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(id="body"):
                yield OverviewPanel(id="main")
            yield Footer()

        def on_mount(self) -> None:
            panel = self.query_one("#main")
            panel.refresh_data()

        def action_show_panel(self, key: str) -> None:
            if key == self.current:
                return
            body = self.query_one("#body", VerticalScroll)
            body.remove_children()
            self.current = key
            widget_cls = PANELS[key]
            widget = widget_cls(id="main")
            body.mount(widget)
            widget.refresh_data()

        def action_refresh_panel(self) -> None:
            panel = self.query_one("#main")
            if hasattr(panel, "refresh_data"):
                panel.refresh_data()
            elif isinstance(panel, ProvidersScreen):
                pass

    ZeroTUI().run()
    return 0
