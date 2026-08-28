"""M0 ground: `zero` console entry exists and parses."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from zero.manage.cli import _build_parser, main


def test_zero_help_runs(capsys) -> None:
    # Bug fix: a bare `zero` used to die with argparse's terse
    # "arguments are required" error; it now prints the full help and
    # still exits 2 so scripts can detect the misuse.
    rc = main([])
    assert rc == 2
    out = capsys.readouterr().out
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


# ----------------------------------------------------------------------
# `zero logs` (regression: AttributeError 'Namespace' has no 'lines')
# ----------------------------------------------------------------------
def test_logs_parser_lines_dest() -> None:
    # Bug fix: bare "-n" derived dest "n" while cmd_logs read ns.lines,
    # so every `zero logs` crashed with AttributeError. Both spellings
    # now land on the same `lines` attribute.
    ns = _build_parser().parse_args(["logs"])
    assert ns.lines == 100
    ns = _build_parser().parse_args(["logs", "-n", "5"])
    assert ns.lines == 5
    ns = _build_parser().parse_args(["logs", "--lines", "7"])
    assert ns.lines == 7


def test_logs_tails_zero_log(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)  # force file branch
    (tmp_path / "zero.log").write_text(
        "\n".join(f"line{i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )
    assert main(["logs", "-n", "3"]) == 0
    assert capsys.readouterr().out.split() == ["line18", "line19", "line20"]


def test_logs_zero_lines_prints_nothing(tmp_path, monkeypatch, capsys) -> None:
    # Python's `[-0:] == [0:]` trap: `zero logs -n 0` used to dump the
    # entire log file. It now prints nothing.
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    (tmp_path / "zero.log").write_text("only\n", encoding="utf-8")
    assert main(["logs", "-n", "0"]) == 0
    assert capsys.readouterr().out == ""


def test_logs_no_log_file_yet(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert main(["logs"]) == 0
    assert "no log file yet" in capsys.readouterr().out


def test_logs_file_fallback_when_unit_absent(tmp_path, monkeypatch, capsys) -> None:
    # journalctl may exist while the zero systemd unit does not (process
    # mode): `zero logs` must read zero.log instead of showing
    # journalctl's "No entries".
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemctl")  # fake path
    (tmp_path / "zero.log").write_text("a\nb\n", encoding="utf-8")
    assert main(["logs", "-n", "1"]) == 0
    assert capsys.readouterr().out.strip() == "b"


# ----------------------------------------------------------------------
# service status (regression: os.kill(pid, 0) KILLS the service on
# Windows — TerminateProcess semantics — so `zero status` stopped the
# bot it was merely checking)
# ----------------------------------------------------------------------
def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_status_reports_stale_pid_without_raising(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)  # no systemctl branch
    (tmp_path / "zero.pid").write_text(str(_dead_pid()), encoding="utf-8")
    assert main(["status", "--json"]) == 0
    out = capsys.readouterr().out
    assert "stopped" in out
    assert "stale" in out


def test_status_reports_running_pid(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    (tmp_path / "zero.pid").write_text(str(os.getpid()), encoding="utf-8")  # pytest is alive
    assert main(["status", "--json"]) == 0
    assert "running" in capsys.readouterr().out


def test_status_ignores_garbage_pid_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    (tmp_path / "zero.pid").write_text("not-a-pid\n", encoding="utf-8")
    assert main(["status", "--json"]) == 0
    assert "stopped" in capsys.readouterr().out


# ----------------------------------------------------------------------
# top-level prompt robustness
# ----------------------------------------------------------------------
def test_main_eof_is_clean_usage_error(tmp_path, monkeypatch, capsys) -> None:
    # getpass with a closed/piped stdin raises EOFError; main() used to
    # let it escape as a raw traceback. It now exits 2 with a message.
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("zero.manage.cli.getpass.getpass", lambda _prompt: (_ for _ in ()).throw(EOFError()))
    assert main(["telegram", "add-bot", "--token-file", ""]) == 2
    assert "EOF" in capsys.readouterr().err


def test_providers_add_probe_flag_is_toggleable() -> None:
    # `--probe` used to be store_true with default=True: a no-op that
    # could never disable probing. BooleanOptionalAction adds --no-probe.
    ns = _build_parser().parse_args(["providers", "add", "--id", "x", "--base-url", "http://x"])
    assert ns.probe is True
    ns = _build_parser().parse_args(
        ["providers", "add", "--id", "x", "--base-url", "http://x", "--no-probe"]
    )
    assert ns.probe is False
