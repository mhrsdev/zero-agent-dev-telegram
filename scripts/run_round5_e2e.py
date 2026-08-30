"""Round-5 E2E runner — boots the REAL engine as a detached subprocess,
waits for health, runs the REAL-credentials drive, collects the exit
status, tears the engine down. Survives across tool invocations because
the engine lifetime is bound to THIS process, not to a shell."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path("/home/z/my-project/zero-agent-dev-telegram")
VENV = Path("/home/z/my-project/venv/bin/python")
LOG = REPO / "realrun-evidence" / "round5" / "engine.log"
HOME_DIR = Path("/home/z/my-project/zero-e2e-home")


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    engine = subprocess.Popen(
        [
            "bash",
            str(REPO / "scripts" / "e2e_round5_engine.sh"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from THIS process group
        cwd=str(REPO),
    )
    print(f"engine pid={engine.pid}", flush=True)
    try:
        healthy = False
        for _ in range(30):
            if engine.poll() is not None:
                print(f"ENGINE DIED during boot (rc={engine.returncode})", flush=True)
                return 2
            try:
                r = httpx.get("http://127.0.0.1:8010/healthz", timeout=2.0)
                if r.status_code == 200:
                    healthy = True
                    print("engine healthy", flush=True)
                    break
            except Exception:
                time.sleep(1.0)
        if not healthy:
            print("engine never became healthy", flush=True)
            return 3

        drive = subprocess.run(
            [str(VENV), str(REPO / "scripts" / "e2e_round5_drive.py")],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            # Round 7: the drive now includes the FULL approval boundary
            # matrix (second actionable intake + reject + forged token +
            # stranger + replay) on top of the real execution/decomposition
            # waits — 600s was too tight for two real planner runs plus a
            # real multi-task agent execution.
            timeout=1800,
        )
        tail = (drive.stdout or "").splitlines()[-30:]
        print("\n".join(tail), flush=True)
        if drive.returncode != 0:
            print("DRIVE STDERR tail:", flush=True)
            print((drive.stderr or "").splitlines()[-15:], flush=True)
        return drive.returncode
    finally:
        try:
            os.killpg(os.getpgid(engine.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            engine.wait(timeout=10)
        except Exception:
            try:
                os.killpg(os.getpgid(engine.pid), signal.SIGKILL)
            except Exception:
                pass
        print("engine stopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
