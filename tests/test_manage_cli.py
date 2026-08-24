"""M0 ground: `zero` console entry exists and parses."""

from __future__ import annotations

import pytest

from zero.manage.cli import _build_parser, main


def test_zero_help_runs(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2  # subcommand now required
    out = capsys.readouterr().err
    assert "usage: zero" in out


def test_zero_status_json_smoke(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    assert main(["status", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"service"' in out


def test_zero_version_flag_exits_clean() -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["--version"])
    assert exc.value.code == 0
