"""Zero Dev Telegram full TUI (Textual).

Screens: Overview / Telegram / Groups / Providers / Usage / System /
Backups / Diagnostics. All payloads come from the pure data layer in
``zero.manage.tui.data``; the app is presentation only. Keyboard-first:
1-9 jump sections, r refreshes, q quits.
"""

from __future__ import annotations

from typing import ClassVar


def panel_base() -> str:
    """Admin GUI base URL; ZERO_PANEL_PORT matches zero.main.resolve_bind."""
    import os

    return "http://127.0.0.1:" + os.environ.get("ZERO_PANEL_PORT", "8000")


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
        """Base panel: pulls a fresh payload from the data layer.

        NOTE: the per-panel hook is deliberately named ``_render_payload``.
        Textual >= 0.86 introduced its own ``Widget._render()`` framework
        hook (called by the compositor with NO arguments), so any subclass
        overriding ``_render(self, payload)`` crashed layout with
        "TypeError: _render() missing 1 required positional argument".
        """

        source = "overview"  # data.<source>() name

        def on_mount(self) -> None:
            self.refresh_data()

        def refresh_data(self) -> None:
            try:
                payload = getattr(data, self.source)()
                body = self._render_payload(payload)
            except Exception as exc:  # noqa: BLE001 - a broken payload must
                # never take the whole TUI down (compositor re-enters render
                # during layout, so exceptions here are fatal crashes).
                body = f"[data error] {type(exc).__name__}: {exc}"
            self.update(body)

        def _render_payload(self, payload):
            return str(payload)

    class OverviewPanel(_Panel):
        BORDER_TITLE = "Overview"

        def _render_payload(self, o):
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

        def _render_payload(self, t):
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

        def _render_payload(self, rows):
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

        def _render_payload(self, rows):
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

        def _render_payload(self, s):
            return (
                f"python      : {s['python']}\n"
                f"disk free   : {s['disk_free_gb']} GB\n"
                f"service     : {s['service_kind']}\n"
                f"config home : {s['config_home']}"
            )

    class BackupsPanel(_Panel):
        source = "backups_screen"
        BORDER_TITLE = "Backups"

        def _render_payload(self, b):
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

        def _render_payload(self, report):
            sym = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
            body = "\n".join(
                f"{sym[c['status']]} {c['name']}: {c['detail']}" for c in report["checks"]
            )
            s = report["summary"]
            return f"{body}\n\n{s['total']} checks · {s['fail']} fail · {s['warn']} warn"

    class ChatStreamPanel(Static):
        """GAP 5: streams one execution's tokens into a scrollable pane."""

        BORDER_TITLE = "Chat / Execution stream"

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._exec_id = ""
            self._text = ""

        def on_mount(self) -> None:
            self.update("enter an execution id below and press Enter")

        def start_stream(self, exec_id: str, cookie) -> None:
            import threading

            self._exec_id = exec_id
            self._text = ""
            self._append("[connecting…]")
            if cookie is None:
                self._append("\n[admin login failed — check password / GUI running]")
                return

            def pump():
                import json

                import httpx

                try:
                    with (
                        httpx.Client(timeout=None) as client,
                        client.stream(
                            "GET",
                            f"{panel_base()}/admin/executions/{exec_id}/stream",
                            cookies={"zero_admin": cookie},
                        ) as response,
                    ):
                        if response.status_code != 200:
                            self._append(f"\n[stream unavailable ({response.status_code})]")
                            return
                        for line in response.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                ev = json.loads(line[6:])
                            except ValueError:
                                continue
                            kind = ev.get("type")
                            if kind == "text_delta":
                                self._append(str(ev.get("text", "")))
                            elif kind == "tool_call":
                                self._append(f"[tool:{ev.get('name')}] ")
                            elif kind == "done":
                                self._append("\n[done]")
                                return
                except Exception as exc:  # noqa: BLE001 - network failures are status
                    self._append(f"\n[{type(exc).__name__}]")

            threading.Thread(target=pump, daemon=True).start()

        def _append(self, text: str) -> None:
            self._text += text
            tail = self._text[-8000:]
            self.call_later(lambda: self.update(tail))

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

    def _admin_login(password: str):
        """Log into the local admin GUI and return the session cookie."""
        import httpx

        try:
            response = httpx.post(
                panel_base() + "/admin/login",
                data={"secret": password},
                follow_redirects=False,
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001 - GUI not running
            return None
        if response.status_code != 303:
            return None
        return response.cookies.get("zero_admin")

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
            ("9", "show_panel('chat')", "Chat"),
            ("r", "refresh_panel", "Refresh"),
            ("q", "quit", "Quit"),
        ]

        current: ClassVar[str] = "overview"

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(id="body"):
                yield OverviewPanel()
            yield Footer()

        def _show_panel(self, key: str, force: bool = False) -> None:
            if key == self.current and not force:
                return
            body = self.query_one("#body", VerticalScroll)
            self.current = key
            if key == "chat":
                from textual.containers import Vertical
                from textual.widgets import Input

                holder = Vertical()
                # Mount the holder into the DOM first (children can only
                # be mounted into an already-mounted widget), THEN fill
                # it. Old panels are pruned by explicit reference —
                # remove_children() is asynchronous in Textual >= 1.0, so
                # the old remove-first-then-mount order crashed every
                # panel switch with DuplicateIds.
                previous = list(body.children)
                body.mount(holder)
                if previous:
                    body.remove_children(previous)
                stream = ChatStreamPanel()
                pw_input = Input(placeholder="admin password", password=True)
                exec_input = Input(placeholder="execution id (exec_…)", id="chat-exec")
                holder.mount(exec_input)
                holder.mount(pw_input)
                holder.mount(stream)

                def handle_submitted(message) -> None:
                    import threading

                    from textual.widgets import Input as _Input

                    if not isinstance(message, _Input.Submitted):
                        return
                    if getattr(message.input, "id", "") != "chat-exec":
                        return
                    exec_value = message.value.strip()
                    if not exec_value:
                        return

                    def worker() -> None:
                        # Blocking HTTP login must stay off the UI event
                        # loop or the whole TUI freezes until timeout.
                        cookie = _admin_login(pw_input.value)
                        stream.start_stream(exec_value, cookie)

                    threading.Thread(target=worker, daemon=True).start()

                self._chat_handler = handle_submitted
                exec_input.focus()
                return
            widget_cls = PANELS[key]
            widget = widget_cls()
            previous = list(body.children)
            body.mount(widget)
            if previous:
                body.remove_children(previous)
            if hasattr(widget, "refresh_data"):
                widget.refresh_data()

        def action_show_panel(self, key: str) -> None:
            self._show_panel(key)

        def on_input_submitted(self, event) -> None:
            handler = getattr(self, "_chat_handler", None)
            if handler is not None:
                handler(event)

        def action_refresh_panel(self) -> None:
            # Re-mount the current panel fresh. The old per-widget branch
            # never refreshed the providers DataTable at all.
            self._show_panel(self.current, force=True)

    ZeroTUI().run()
    return 0
