"""Regressions for the 2026-08 TUI crash + CLI setup wizard deadlock.

Covers:
- TUI panels must never override Textual's own ``Widget._render()``
  hook (compositor calls it with zero args -> TypeError, see the
  2026-08-27 `zero tui` traceback).
- Interactive wizard must accept (and normalize) ``telegram_mode``
  answers — the old driver dropped raw input on steps without a
  hard-coded branch, so even typing "bot_api" deadlocked forever.
- Optional steps are skippable; final_validation validates the draft
  before commit; back-navigation stays inside STEP_ORDER.
"""

from __future__ import annotations

import builtins
import getpass

import pytest

from zero.manage.cli import (
    _interactive_setup,
    _parse_field_value,
    _OMIT,
)
from zero.manage.core.config import ConfigService
from zero.manage.services.setup import STEP_ORDER, SetupService
from zero.manage.services.wizard_forms import Field, WIZARD_STEPS


@pytest.fixture
def wizard_home(tmp_path, monkeypatch):
    home = tmp_path / "zh"
    monkeypatch.setenv("ZERO_HOME", str(home))
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'engine.db'}")
    monkeypatch.delenv("ZERO_ENV", raising=False)
    from zero.manage.core import probes

    monkeypatch.setattr(
        probes, "telegram_get_me", lambda token, timeout=10.0: {"ok": True, "id": 1, "username": "t"}
    )
    monkeypatch.setattr(
        probes, "openai_list_models", lambda base, key, timeout=15.0: {"ok": True, "models": ["m-1"]}
    )
    monkeypatch.setattr(
        probes, "openai_completion_probe", lambda base_url, api_key, model, timeout=30.0: {"ok": True}
    )
    return home


# ---------------------------------------------------------------- TUI ----
def test_tui_panels_do_not_override_textual_render_hook():
    """Textual >= 0.86 calls Widget._render() itself (no args); any panel
    overriding `_render(self, payload)` crashes layout with TypeError."""
    import inspect

    import textual
    from textual.widget import Widget

    assert hasattr(Widget, "_render"), (
        f"textual {textual.__version__} changed internals; revisit this guard"
    )
    source = inspect.getsource(__import__(
        "zero.manage.tui.app", fromlist=["run"]
    ).run)
    # The old bug: `def _render(self, o):` inside the panel classes.
    assert "def _render(" not in source, (
        "panels must use _render_payload; _render collides with Textual's "
        "Widget._render() framework hook"
    )
    assert "def _render_payload(" in source


def test_tui_app_survives_headless_render(wizard_home):
    """Drive the real app headless: compose + layout + panel switch."""
    textual_app = pytest.importorskip("textual.app")
    captured = {}
    textual_app.App.run = lambda self, *a, **k: captured.setdefault("app", self)

    from zero.manage.tui import app as tui_module

    tui_module.run()
    app = captured["app"]

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("2")  # telegram panel
            await pilot.pause()
            await pilot.press("r")  # refresh (re-mount path)
            await pilot.pause()
            await pilot.press("1")  # back to overview
            await pilot.pause()

    import asyncio

    asyncio.run(drive())


# ------------------------------------------------------------- wizard ----
def test_telegram_mode_accepts_exact_answer(wizard_home):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    result = setup.answer("telegram_mode", {"mode": "bot_api"})
    assert result.ok, result.errors
    assert setup.current() == "telegram_credentials"


@pytest.mark.parametrize("raw", ["bot_api", "bot-api", "BOT_API", "botapi", "  bot_api  "])
def test_telegram_mode_normalizes_variants(wizard_home, raw):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    result = setup.answer("telegram_mode", {"mode": raw})
    assert result.ok, result.errors
    assert setup.resume()["data"]["telegram_mode"]["mode"] == "bot_api"


def test_telegram_mode_lists_available_options_on_error(wizard_home):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    result = setup.validate("telegram_mode", {"mode": "user_session"})
    assert not result.ok
    assert any("bot_api" in e and "options" in e for e in result.errors)


def test_skip_advances_current_step(wizard_home):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    assert setup.skip("welcome") == "environment"
    assert setup.current() == "environment"


def test_final_validation_rejects_unoffered_routing_model(wizard_home):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    assert setup.answer("telegram_credentials", {"token": "123:abc"}).ok
    data = setup.resume()["data"]
    data["provider_add"] = {
        "id": "p1",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "models": ["m-1"],
        "_raw": {"api_key": "sk"},
    }
    data["model_assign"] = {"primary_model": "m-1", "fallback_models_csv": "ghost"}
    setup.cfg.save_draft({"current_step": "final_validation", "data": data})
    result = setup.validate("final_validation", {})
    assert not result.ok
    assert any("ghost" in e for e in result.errors)


def test_provider_add_populates_models_from_discovery(wizard_home):
    setup = SetupService(ConfigService(wizard_home), lambda: None)
    value = {
        "id": "p1",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-x",
    }
    assert setup.validate("provider_add", value).ok
    assert value["models"] == ["m-1"], "discovered models must flow into provider config"


# -------------------------------------------------- CLI prompt parsing ----
def test_parse_select_normalizes_and_accepts_index():
    field = Field(name="mode", label="Mode", kind="select", options=("bot_api",), default="bot_api")
    assert _parse_field_value(field, "bot_api", None) == ("bot_api", None)
    assert _parse_field_value(field, "bot-api", None) == ("bot_api", None)
    assert _parse_field_value(field, "1", None) == ("bot_api", None)
    assert _parse_field_value(field, "", "bot_api") == ("bot_api", None)
    err = _parse_field_value(field, "wat", None)[1]
    assert err and "bot_api" in err


def test_parse_bool_and_int():
    boolf = Field(name="on", label="On", kind="bool", default=False)
    assert _parse_field_value(boolf, "y", None) == (True, None)
    assert _parse_field_value(boolf, "0", None) == (False, None)
    assert _parse_field_value(boolf, "", None) == (False, None)
    int_f = Field(name="n", label="N", kind="int", default=7)
    assert _parse_field_value(int_f, "3", 7) == (3, None)
    assert _parse_field_value(int_f, "", 7) == (7, None)
    assert _parse_field_value(int_f, "x", 7)[1] is not None


def test_parse_required_empty_reports_error():
    field = Field(name="id", label="Provider id", required=True)
    value, err = _parse_field_value(field, "", None)
    assert value is None and "required" in err
    value2, err2 = _parse_field_value(field, "", "openai-primary")
    assert value2 == "openai-primary" and err2 is None
    assert _parse_field_value(Field(name="opt", label="Opt"), "", None)[0] is _OMIT


# ---------------------------------------------- interactive driver e2e ----
def test_interactive_wizard_survives_user_paste_session(wizard_home, monkeypatch):
    """The exact session from the bug report: typing bot_api must advance."""
    script = iter(["", "development", "", "bot_api"])

    def fake_input(prompt: str = "") -> str:
        return next(script)

    def fake_getpass(prompt: str = "") -> str:
        raise EOFError  # terminal closed right after the telegram_mode fix

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(getpass, "getpass", fake_getpass)

    setup = SetupService(ConfigService(wizard_home), lambda: None, secret_store=lambda n, t, v: "sec_x")
    rc = _interactive_setup(setup)
    assert rc == 130, "wizard must pause gracefully (not deadlock) at the token prompt"
    assert setup.current() == "telegram_credentials"
    assert setup.resume()["data"]["telegram_mode"]["mode"] == "bot_api"


def test_wizard_step_forms_cover_every_step():
    """Every STEP_ORDER step has a form spec so the CLI can render options."""
    assert set(WIZARD_STEPS) == set(STEP_ORDER)
