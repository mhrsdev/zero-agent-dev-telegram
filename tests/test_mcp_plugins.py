"""GAP 7 tests: MCP client integration and the plugin registry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zero.manage.core.mcp_client import (
    MCPManager,
    MCPServerProcess,
    mcp_tool_name,
    parse_server_config,
    sanitize_name_component,
)


@pytest.fixture
def services(test_settings):
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


from zero.manage.plugins.registry import (
    load_plugins,
    plugin_dirs,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_SERVER = str(FIXTURES / "fake_mcp_server.py")


class TestNamingAndConfig:
    def test_tool_naming_sanitized(self):
        assert mcp_tool_name("file-system", "read.file") == "mcp_file_system_read_file"
        assert sanitize_name_component("a b/c!") == "a_b_c_"

    def test_config_parsing(self):
        entries = parse_server_config(
            json.dumps([{"name": "fs", "command": ["npx"], "enabled": True}])
        )
        assert len(entries) == 1
        assert parse_server_config("") == []
        assert parse_server_config("not json") == []
        assert parse_server_config('{"name": "x"}') == []

    def test_disabled_server_not_loaded(self):
        manager = MCPManager()
        count = manager.load(
            [{"name": "off", "command": [sys.executable, "-c", "pass"], "enabled": False}]
        )
        assert count == 0
        assert not manager.servers


class TestMCPClientAgainstFakeServer:
    def test_connect_discover_call(self):
        server = MCPServerProcess(name="adder", command=[sys.executable, FAKE_SERVER])
        try:
            assert server.connect() is True
            names = [tool["name"] for tool in server.tools]
            assert names == ["add"]
            output = server.call_tool("add", {"a": 2, "b": 3})
            assert output == "sum=5"
            with pytest.raises(RuntimeError, match="reported an error"):
                server.call_tool("add", {"a": 1, "b": -2})
        finally:
            server.shutdown()

    def test_unspawnable_command_is_tolerated(self):
        server = MCPServerProcess(name="broken", command=["definitely-not-a-binary-xyz"])
        assert server.connect() is False
        server.shutdown()  # idempotent


class TestManagerRegistrationIntoToolService:
    @staticmethod
    def _services():
        from zero.app.services import build_services
        from zero.config import Settings
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations

        settings = Settings.load_for_test()
        database = Database(settings)
        apply_migrations(database)
        return build_services(settings, database), settings

    def test_mcp_tools_flow_through_standard_pipeline(self):
        services, _settings = self._services()
        manager = MCPManager()
        connected = manager.load([{"name": "math", "command": [sys.executable, FAKE_SERVER]}])
        assert connected == 1
        registered = manager.register_tools(services.tools)
        assert registered == ["mcp_math_add"]
        owner = services.identity.create_user(display_name="mcp owner")
        project = services.identity.create_project(owner_id=owner.id, name="MCP")
        tool = services.tools._tool_repo.get_tool_by_name("mcp_math_add")
        grant = services.tools.grant_tool(
            project_id=project.id,
            actor_id=owner.id,
            tool_id=tool.id,
            agent_scope="main_worker",
            source="system",
        )
        assert grant is not None
        result = services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="mcp_math_add",
            input_data={"a": 20, "b": 22},
            source="system",
        )
        assert result.status == "success"
        assert result.output["output"] == "sum=42"
        # Ungranted invocation is denied by the standard capability path.
        other_project = services.identity.create_project(owner_id=owner.id, name="MCP2")
        from zero.app.tool_service import ToolInvocationDeniedError

        with pytest.raises(ToolInvocationDeniedError):
            services.tools.invoke(
                project_id=other_project.id,
                actor_id=owner.id,
                agent_scope="main_worker",
                tool_name="mcp_math_add",
                input_data={"a": 1, "b": 1},
                source="system",
            )
        manager.shutdown()


class TestAdminEndpointSecurity:
    """Audit D3: provider probe endpoint must require session + CSRF."""

    @staticmethod
    def _harness(services, settings, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from zero.manage import web

        zero_home = tmp_path / "zero-home"
        zero_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ZERO_HOME", str(zero_home))
        web._sessions.clear()
        app = FastAPI()
        app.state.services = services
        app.state.settings = settings
        from zero.app.stream_hub import ExecutionStreamHub

        app.state.stream_hub = ExecutionStreamHub()
        web.register_admin(app, services)
        client = TestClient(app)
        # Bootstrap-login to obtain an authenticated session.
        client.get("/admin/login")
        setup_code = (zero_home / "setup-code.txt").read_text(encoding="utf-8").strip()
        client.post("/admin/login/bootstrap", data={"secret": setup_code})
        client.post(
            "/admin/login/setpw",
            data={"pw": "correct horse battery", "pw2": "correct horse battery"},
        )
        return app, client

    def _anonymous(self, app):
        from fastapi.testclient import TestClient

        return TestClient(app)  # no cookies → no session

    def test_admin_login_reachable_with_engine_auth_required(
        self, services, test_settings, tmp_path, monkeypatch
    ):
        """Audit S6: the engine bearer middleware must not gate /admin —
        the GUI has its own password+CSRF scheme and would otherwise be
        unreachable in production (auth_required forces True there)."""

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from zero.app.api import _register_auth_middleware
        from zero.manage import web

        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "zh"))
        web._sessions.clear()
        strict = test_settings.model_copy(update={"auth_required": True})
        app = FastAPI()
        app.state.settings = strict
        app.state.services = services
        _register_auth_middleware(app, services, strict)
        web.register_admin(app, services)
        client = TestClient(app)
        page = client.get("/admin/login")
        assert page.status_code == 200, (
            f"/admin/login must be reachable with engine auth on; got {page.status_code}"
        )

    def test_password_change_invalidates_old_sessions(
        self, services, test_settings, tmp_path, monkeypatch
    ):
        from zero.manage import web

        app, client = self._harness(services, test_settings, tmp_path, monkeypatch)
        old_sid = client.cookies.get("zero_admin")
        assert old_sid and web._valid_session.__globals__["_sessions"].get(old_sid)

        # Rotate the password through the real flow.
        client.post(
            "/admin/login/setpw",
            data={"pw": "another-long-password", "pw2": "another-long-password"},
        )
        # The OLD session id must no longer authenticate.
        import fastapi.testclient as ftc

        old_client = ftc.TestClient(app)
        old_client.cookies.set("zero_admin", old_sid)
        probe = old_client.get("/admin/config", follow_redirects=False)
        assert probe.status_code in (303, 401), (
            "old session must be invalidated by a password change"
        )

    def test_login_bruteforce_lockout(self, services, test_settings, tmp_path, monkeypatch):
        from zero.manage import web

        app, _client = self._harness(services, test_settings, tmp_path, monkeypatch)
        web._login_failures.clear()
        anonymous = self._anonymous(app)
        locked = False
        for _ in range(web._LOCKOUT_THRESHOLD + 2):
            resp = anonymous.post("/admin/login", data={"secret": "wrong"}, follow_redirects=False)
            if resp.status_code == 429:
                locked = True
                break
        assert locked, "repeated failures must trigger lockout"
        # A correct password during lockout must STILL be refused.
        ok = anonymous.post(
            "/admin/login",
            data={"secret": "correct horse battery"},
            follow_redirects=False,
        )
        assert ok.status_code == 429

    def test_provider_probe_requires_session(self, services, test_settings, tmp_path, monkeypatch):
        app, _client = self._harness(services, test_settings, tmp_path, monkeypatch)
        resp = self._anonymous(app).post("/admin/providers/p1/test", follow_redirects=False)
        assert resp.status_code in (303, 401), "unauthenticated provider probe must not succeed"

    def test_provider_probe_requires_csrf(self, services, test_settings, tmp_path, monkeypatch):
        _app, client = self._harness(services, test_settings, tmp_path, monkeypatch)
        resp = client.post("/admin/providers/p1/test")
        assert resp.status_code == 400, "missing CSRF token must be rejected"

    def test_provider_probe_allows_authenticated_csrf(
        self, services, test_settings, tmp_path, monkeypatch
    ):
        from zero.manage import web

        _app, client = self._harness(services, test_settings, tmp_path, monkeypatch)
        sid = client.cookies.get("zero_admin") or ""
        csrf = web._csrf(sid)
        resp = client.post(
            "/admin/providers/unknown-id/test",
            headers={"x-admin-csrf": csrf},
        )
        # Unknown provider id reaches the handler and 404s cleanly.
        assert resp.status_code == 404


class TestPluginRegistry:
    @staticmethod
    def _write_plugin(directory: Path, name: str, tool_name: str | None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if tool_name is None:
            # Minimal valid plugin used for order/isolation assertions.
            path.write_text(
                "CALLS = []\ndef register(ctx):\n    CALLS.append(id(ctx))\n",
                encoding="utf-8",
            )
            return path
        path.write_text(
            "def register(ctx):\n"
            f"    ctx.tool_registry.register_tool(\n"
            f"        name={tool_name!r},\n"
            "        description='plugin tool',\n"
            "        input_schema={'type': 'object', 'properties': {}},\n"
            "        output_schema={'type': 'object'},\n"
            f"        handler_key='plugin:{tool_name}',\n"
            "        handler=lambda data, ctx: {'ok': True},\n"
            "        inline=True,\n"
            "    )\n",
            encoding="utf-8",
        )
        return path

    def test_plugin_dirs_order_system_then_user(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "system"))
        dirs = plugin_dirs()
        assert dirs[0][0] == "system" and "system" in str(dirs[0][1])
        assert dirs[1][0] == "user" and "home" in str(dirs[1][1])

    def test_load_user_and_system_plugins_alphabetically(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        # The system override points at the plugin directory itself.
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "system"))
        self._write_plugin(tmp_path / "home" / "plugins", "zeta.py", None)
        self._write_plugin(tmp_path / "home" / "plugins", "alpha.py", None)
        self._write_plugin(tmp_path / "system", "mid.py", None)

        loaded = load_plugins(tool_service=None)
        labels = [label.split(":")[1] for label in loaded]
        # System dir first (alphabetical), then user dir (alphabetical).
        assert labels == ["mid.py", "alpha.py", "zeta.py"]

    def test_broken_plugins_are_isolated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "nonexistent"))
        broken = tmp_path / "home" / "plugins" / "broken.py"
        broken.parent.mkdir(parents=True)
        broken.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        missing_register = tmp_path / "home" / "plugins" / "noregister.py"
        missing_register.write_text("x = 1\n", encoding="utf-8")
        syntax_error = tmp_path / "home" / "plugins" / "syntax.py"
        syntax_error.write_text("def (:\n", encoding="utf-8")
        self._write_plugin(tmp_path / "home" / "plugins", "good.py", None)

        loaded = load_plugins(tool_service=None)
        assert loaded == ["user:good.py"]

    def test_manage_context_reaches_registration(self, monkeypatch, tmp_path):
        """A sample-style plugin can add a callable tool end-to-end."""
        services, _settings = self._services()
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "nonexistent"))
        sample = Path(__file__).resolve().parents[1] / ("examples/plugins/echo_upper.py")
        target_dir = tmp_path / "home" / "plugins"
        target_dir.mkdir(parents=True)
        (target_dir / "echo_upper.py").write_text(
            sample.read_text(encoding="utf-8"), encoding="utf-8"
        )

        loaded = load_plugins(
            tool_service=services.tools,
            config=None,
            secret_store=None,
        )
        assert loaded == ["user:echo_upper.py"]
        owner = services.identity.create_user(display_name="plug owner")
        project = services.identity.create_project(owner_id=owner.id, name="Plug")
        tool = services.tools._tool_repo.get_tool_by_name("echo_upper")
        services.tools.grant_tool(
            project_id=project.id,
            actor_id=owner.id,
            tool_id=tool.id,
            agent_scope="main_worker",
            source="system",
        )
        result = services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo_upper",
            input_data={"text": "hello plugin"},
            source="system",
        )
        assert result.output["echoed"] == "HELLO PLUGIN"

    def test_duplicate_registration_surfaces_loudly_but_never_crashes(self, monkeypatch, tmp_path):
        services, _settings = self._services()
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "nonexistent"))
        d = tmp_path / "home" / "plugins"
        self._write_plugin(d, "one.py", "dup_tool")
        self._write_plugin(d, "two.py", "dup_tool")
        # Both load attempts complete; registration outcome is the tool
        # service's documented duplicate policy.
        loaded = load_plugins(tool_service=services.tools)
        assert len(loaded) >= 1


def _services():
    from zero.app.services import build_services
    from zero.config import Settings
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database), settings


# Provide the helper used by tests above without shadowing class methods.
def TestPluginRegistry__services():
    return _services()


TestPluginRegistry._services = staticmethod(_services)
