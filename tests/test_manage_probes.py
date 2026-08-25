"""Regression tests for the manage-layer network probes.

The wizard calls these probes directly, so broken URL construction must
fail loudly here rather than surfacing as a crash inside the interactive
setup flow.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from zero.manage.core.probes import telegram_recent_chats


class _TelegramStubHandler(BaseHTTPRequestHandler):
    """Minimal Bot API stub: empty successful getUpdates response."""

    def do_GET(self):
        body = json.dumps({"ok": True, "result": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep test output quiet


def test_recent_chats_uses_configured_telegram_api_base(monkeypatch) -> None:
    """Group discovery must honor ZERO_TELEGRAM_API_BASE (gateways/tests)."""
    server = HTTPServer(("127.0.0.1", 0), _TelegramStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "ZERO_TELEGRAM_API_BASE",
            f"http://127.0.0.1:{server.server_address[1]}",
        )
        outcome = telegram_recent_chats("123456:stub-token")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert outcome["ok"] is True
    assert outcome["chats"] == []
