"""M0 ground: `zero` console entry exists and parses."""
from __future__ import annotations

import pytest

from zero.manage.cli import _build_parser, main


def test_zero_help_runs(capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Zero Dev Telegram" in out


def test_zero_version_flag_exits_clean() -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["--version"])
    assert exc.value.code == 0
