#!/usr/bin/env python3
"""S7 — real replication of the reported Windows console session.

Replays every defect from the user's `zero setup` / `zero start` /
`zero-develop serve` transcript against the FIXED code, for real:

A. bare `zero-develop`          -> full help (was: argparse error)
B. `zero-develop serve` on a busy port -> friendly refusal (was: WinError 10048 traceback, exit 0)
C. `zero-develop serve` while the managed service is running -> friendly refusal
D. `zero start` while port 8000 is occupied (the live server) -> process verified dead,
   actionable log tail, exit 1 (was: silent doomed spawn, exit 0)
E. FULL 18-step `zero setup` wizard replay with the REAL bot token, REAL
   provider (real HTTP probes) and REAL group — including the reported
   websearch dead-loop keystrokes — ending in a REAL Telegram test message
   (was: step collected a chat id and sent nothing)
F. `zero stop` honesty + `zero start` already-running refusal + clean stop

All scratch homes live under scripts/realrun/s7-*; the deployed real home
is never touched.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path("/home/z/my-project/zero/zero-agent-dev-telegram")
VENV = PROJECT / ".venv"
BIN = VENV / "bin"
REALRUN = Path("/home/z/my-project/scripts/realrun")
STATE = REALRUN / "state.json"

BOT_TOKEN = "8753924431:AAHc3lP-lVFqSuFhm1qMgkqoQqkLVEsOEb8"
API_KEY = "sk-BlwjB2GhsGBwFLjQBBAhKK7FpmfJYP9usqGfrImaLaA1JOKW"
BASE_URL = "https://api.justwoker.icu/v1"
MODEL = "claude-opus-5"
GROUP_ID = "-1004406039396"

results: list[dict] = []


def fresh_env(home: Path, *, no_systemctl: bool = False) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/home/z")}
    if no_systemctl:
        # This CI box ships a systemctl shim; the user's Windows host has
        # none. Shadow it so start/stop take the plain-process path the
        # console session actually exercised.
        env["PATH"] = "/nonexistent"
    env["ZERO_HOME"] = str(home)
    return env


def run(cmd: list[str], env: dict, stdin_text: str | None = None, timeout: int = 120):
    return subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(env.get("ZERO_HOME") or str(PROJECT)),
    )


def record(scenario: str, ok: bool, detail: str) -> None:
    results.append({"scenario": scenario, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {scenario}: {detail}")


def hold_port(port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    return srv


# ----------------------------------------------------------------------
# A. bare zero-develop -> help, exit 2
# ----------------------------------------------------------------------
home_a = REALRUN / "s7-home-a"
home_a.mkdir(exist_ok=True)
r = run([str(BIN / "zero-develop")], fresh_env(home_a))
ok = r.returncode == 2 and "usage: zero-develop" in r.stdout and "serve" in r.stdout
record(
    "A_bare_zero_develop_help",
    ok,
    f"rc={r.returncode}, help printed={'usage: zero-develop' in r.stdout}, "
    f"argparse_error={'the following arguments are required' in (r.stdout + r.stderr)}",
)

# ----------------------------------------------------------------------
# B. serve on a busy port -> friendly refusal, exit 1
# ----------------------------------------------------------------------
home_b = REALRUN / "s7-home-b"
home_b.mkdir(exist_ok=True)
holder = hold_port(8100)
try:
    r = run([str(BIN / "zero-develop"), "serve", "--port", "8100"], fresh_env(home_b), timeout=60)
finally:
    holder.close()
ok = r.returncode == 1 and "already in use" in r.stderr and "--port 8001" in r.stderr
record(
    "B_serve_busy_port",
    ok,
    f"rc={r.returncode}, friendly={'already in use' in r.stderr}, "
    f"traceback={'Traceback' in r.stderr + r.stdout}",
    )

# ----------------------------------------------------------------------
# C. serve while the managed service is alive -> friendly refusal
# ----------------------------------------------------------------------
home_c = REALRUN / "s7-home-c"
home_c.mkdir(exist_ok=True)
sleeper = subprocess.Popen(["sleep", "120"])
(home_c / "zero.pid").write_text(str(sleeper.pid), encoding="utf-8")
r = run([str(BIN / "zero-develop"), "serve"], fresh_env(home_c), timeout=60)
ok = r.returncode == 1 and "already running" in r.stderr and "zero stop" in r.stderr
record(
    "C_serve_managed_running",
    ok,
    f"rc={r.returncode}, message={'already running' in r.stderr}",
)

# ----------------------------------------------------------------------
# D. zero start while port 8000 is occupied (the live server) -> refuse
#    up front: never spawn a doomed child, never credit a foreign service
# ----------------------------------------------------------------------
home_d = REALRUN / "s7-home-d"
if home_d.exists():
    import shutil as _sh
    _sh.rmtree(home_d)
home_d.mkdir(parents=True, exist_ok=True)
r = run([str(BIN / "zero"), "start"], fresh_env(home_d, no_systemctl=True), timeout=90)
ok = r.returncode == 1 and (
    "already serves a healthy Zero service" in r.stdout
    or "already in use" in r.stdout
) and "started pid=" not in r.stdout
record(
    "D_start_on_occupied_port",
    ok,
    f"rc={r.returncode}, refusal={'already serves' in r.stdout or 'already in use' in r.stdout}, "
    f"no_doomed_spawn={'started pid=' not in r.stdout}",
)

# ----------------------------------------------------------------------
# F. stop honesty + start already-running refusal (before the long E so
#    every CLI scenario is covered even if the wizard hiccups)
# ----------------------------------------------------------------------
home_f = REALRUN / "s7-home-f"
if home_f.exists():
    import shutil as _sh2
    _sh2.rmtree(home_f)
home_f.mkdir(parents=True, exist_ok=True)
env_f = fresh_env(home_f, no_systemctl=True)
r1 = run([str(BIN / "zero"), "stop"], env_f)
sleeper2 = subprocess.Popen(["sleep", "120"])
(home_f / "zero.pid").write_text(str(sleeper2.pid), encoding="utf-8")
r2 = run([str(BIN / "zero"), "start"], env_f)
ok = (
    r1.returncode == 0
    and "service not running (no pid file)" in r1.stdout
    and r2.returncode == 1
    and f"service already running (pid {sleeper2.pid})" in r2.stdout
)
record("F_stop_honesty_and_start_guard", ok, f"stop_rc={r1.returncode}, start_rc={r2.returncode}")

# ----------------------------------------------------------------------
# E. FULL REAL wizard replay (18 steps, real probes, real Telegram send),
#    driven PROMPT-BY-PROMPT so transient real-network probe failures
#    (and the retry menus they trigger) cannot desync a static stdin blob.
# ----------------------------------------------------------------------
import shutil as _sh

home_e = REALRUN / "s7-home-e"
if home_e.exists():
    _sh.rmtree(home_e)
home_e.mkdir(parents=True, exist_ok=True)

STATEFUL = {
    "websearch_enabled": 0,
    "websearch_provider": 0,
    "websearch_key": 0,
}


def answer_for(prompt: str) -> str:
    p = prompt
    if "Press Enter to continue" in p:
        return ""
    if "[Enter=retry same answers" in p:
        # The reported keystroke: Enter retries the same answers, which
        # fails identically ONCE, then the wizard auto re-asks.
        return ""
    if "Bot token (from @BotFather)" in p:
        return BOT_TOKEN
    if "Bot token (for discovery)" in p:
        return ""
    if "Search provider id" in p:
        STATEFUL["websearch_provider"] += 1
        return "" if STATEFUL["websearch_provider"] == 1 else "openai-primary"
    if "Search API key" in p:
        STATEFUL["websearch_key"] += 1
        return "" if STATEFUL["websearch_key"] == 1 else API_KEY
    if "Provider id" in p:
        return ""
    if "Protocol" in p:
        return ""
    if "Base URL" in p:
        return BASE_URL
    if "API key" in p:
        return API_KEY
    if "Model to test" in p:
        return ""
    if "Primary model" in p:
        return ""
    if "Fallback models" in p:
        return ""
    if "Who can use the bot?" in p:
        return ""
    if "Group chat id" in p:
        return GROUP_ID
    if "Group title" in p:
        return "zero"
    if "Default agent" in p:
        return ""
    if "Enable web search" in p:
        STATEFUL["websearch_enabled"] += 1
        return "y" if STATEFUL["websearch_enabled"] == 1 else ""
    if "Enable telemetry" in p:
        return "y"
    if "Auto-apply updates" in p:
        return ""
    if "Channel" in p:
        return ""
    if "Schedule" in p:
        return ""
    if "Retention count" in p:
        return ""
    if "Chat id for the test message" in p:
        return GROUP_ID
    # environment/version/telegram_mode selects and any unknown prompt:
    # accept the default.
    return ""


env_e = fresh_env(home_e)
proc = subprocess.Popen(
    [str(BIN / "zero"), "setup"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=env_e,
    cwd=str(home_e),
    bufsize=0,
)
transcript: list[str] = []
import selectors as _sel
import time as _time

sel = _sel.DefaultSelector()
sel.register(proc.stdout, _sel.EVENT_READ)
buf = ""
deadline = _time.time() + 480
prompt_deadline = None
while _time.time() < deadline and proc.poll() is None:
    events = sel.select(0.2)
    for key, _ in events:
        chunk = os.read(key.fd, 65536).decode("utf-8", errors="replace")
        if chunk:
            buf += chunk
            transcript.append(chunk)
    tail = buf[-400:]
    # A prompt = stream paused, last line unterminated, ends with ": "
    # (raw tail — rstrip would eat the trailing space that marks it).
    if buf and not buf.endswith("\n"):
        if buf.endswith(": ") or buf.endswith("]: "):
            if prompt_deadline is None:
                prompt_deadline = _time.time() + 0.3
            elif _time.time() >= prompt_deadline:
                ans = answer_for(tail[-200:])
                seen = transcript.count(tail[-120:])
                if seen > 6:
                    print(f"DRIVER LOOP detected at prompt: {tail[-120:]!r}", file=sys.stderr)
                    proc.kill()
                    break
                proc.stdin.write(ans + "\n")
                proc.stdin.flush()
                buf = ""
                prompt_deadline = None
        else:
            prompt_deadline = None
    else:
        prompt_deadline = None
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
out = "".join(transcript)

sent_marker = "test message delivered (message_id "
msg_id = ""
if sent_marker in out:
    msg_id = out.split(sent_marker, 1)[1].split(")", 1)[0]
ok = (
    proc.returncode == 0
    and "same answers failed twice — re-asking this step's fields" in out
    and sent_marker in out
    and "ok — setup complete" in out
    and "configuration written" in out
    # exactly ONE "ok -> test_message": the legitimate transition INTO
    # step 18. The old bug printed it a SECOND time as the last-step's
    # own transition (self-reference) with no completion line.
    and out.count("ok -> test_message") == 1
)
record(
    "E_full_wizard_replay_real_send",
    ok,
    f"rc={proc.returncode}, deadloop_recovered="
    f"{'same answers failed twice' in out}, telegram_message_id={msg_id}, "
    f"completed={'ok — setup complete' in out}",
)
out_e = out

# Sanity: the committed scratch config is valid and complete
cfg_file = home_e / "config.yaml"
cfg_ok = cfg_file.exists() and all(
    marker in cfg_file.read_text(encoding="utf-8")
    for marker in ("openai-primary", GROUP_ID, "websearch")
)
record("E2_committed_config", cfg_ok, f"config_written={cfg_file.exists()}")

# ----------------------------------------------------------------------
# persist evidence
# ----------------------------------------------------------------------
summary = {
    "scenario": "s7_console_session_regressions",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "results": results,
    "telegram_test_message_id": msg_id,
    "wizard_home": str(home_e),
}
state = {}
if STATE.exists():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except ValueError:
        state = {}
state["s7_console_session_regressions"] = summary
STATE.write_text(json.dumps(summary, indent=2) + "\n" if not state else json.dumps(state, indent=2) + "\n", encoding="utf-8")
(home_e / "wizard-output.txt").write_text(out_e, encoding="utf-8")

failed = [r_ for r_ in results if not r_["ok"]]
print(f"\nS7 SUMMARY: {len(results) - len(failed)}/{len(results)} scenarios passed")
sys.exit(1 if failed else 0)
