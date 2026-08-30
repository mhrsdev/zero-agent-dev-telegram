"""Round-8 E2E runner — boots the REAL engine as a detached subprocess,
waits for health, runs the chosen profile drive, tears the engine down.

Mirrors run_round5_e2e.py; the engine lifetime is bound to THIS process
so the sandbox cannot reap it between tool calls.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path("/home/z/my-project/workspace/zero-agent-dev-telegram")
VENV = REPO / ".venv" / "bin" / "python"
LOG = REPO / "realrun-evidence" / "round8" / f"engine-{os.environ.get('E2E_PROFILE', 'live')}.log"
HOME_DIR = Path("/home/z/my-project/zero-e2e-home")


def main() -> int:
    profile = os.environ.get("E2E_PROFILE", "live").strip().lower()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "ZERO_HOME": str(HOME_DIR),
            "ZERO_ENV": "development",
            "ZERO_DATABASE_URL": f"sqlite:///{HOME_DIR/'e2e.db'}",
            "ZERO_SECRET_KEY": "e" * 64,
            "ZERO_TELEGRAM_WEBHOOK_SECRET": "e2e-webhook-secret-9f31c2",
            "ZERO_DECOMPOSITION_ENABLED": "1",
            "ZERO_TASK_MAX_ATTEMPTS": "4",
            "PYTHONPATH": str(REPO / "src"),
        }
    )
    engine = subprocess.Popen(
        [
            str(VENV),
            "-m",
            "uvicorn",
            "zero.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
        ],
        stdout=open(LOG, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(REPO),
        env=env,
    )
    print(f"engine pid={engine.pid} profile={profile}", flush=True)
    try:
        healthy = False
        for _ in range(40):
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

        # Give the polling worker one long-poll cycle to come online.
        time.sleep(6)
        drive = subprocess.run(
            [str(VENV), str(REPO / "scripts" / "e2e_round8_drive.py")],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=1560 if profile == "exec" else 600,
        )
        tail = (drive.stdout or "").splitlines()[-40:]
        print("\n".join(tail), flush=True)
        if drive.returncode != 0:
            print("DRIVE STDERR tail:", flush=True)
            print((drive.stderr or "").splitlines()[-20:], flush=True)
        return drive.returncode
    except subprocess.TimeoutExpired:
        print("DRIVE TIMED OUT", flush=True)
        return 4
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
