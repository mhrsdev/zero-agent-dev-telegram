"""Shared bootstrap for the REAL full-feature run.

Sets engine env BEFORE zero imports, loads real Settings, builds the real
Services bundle (openai-compatible adapter on the user's provider, real
httpx outbound, policy gate from the real config.yaml, worktree tools,
plugins, delegation, LLM planner/decomposer/compaction-summarizer).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REAL_HOME = Path("/home/z/my-project/zero-real-home")
WORKSPACE = Path("/home/z/my-project/zero-workspace")
REPO = WORKSPACE / "textkit-repo"
STATE = Path("/home/z/my-project/scripts/realrun/state.json")

BOT_TOKEN = os.environ.get("REALRUN_BOT_TOKEN", "")
API_KEY = os.environ.get("REALRUN_API_KEY", "")
GROUP_ID = "-1004406039396"
MODEL = "claude-opus-5"
TG_SENDER_ID = "777000001"  # synthetic-but-verified telegram external id for the owner


def setup_env() -> None:
    if not BOT_TOKEN or not API_KEY:
        raise RuntimeError(
            "set REALRUN_BOT_TOKEN and REALRUN_API_KEY "
            "(real credentials are no longer embedded in this module)"
        )
    REAL_HOME.mkdir(parents=True, exist_ok=True)
    (REAL_HOME / "worktrees").mkdir(exist_ok=True)
    (REAL_HOME / "plugins").mkdir(exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    os.environ["ZERO_ENV"] = "development"
    os.environ["ZERO_HOME"] = str(REAL_HOME)
    os.environ["ZERO_DATABASE_URL"] = f"sqlite:///{REAL_HOME}/engine.db"
    os.environ["ZERO_OPENAI_API_KEY"] = API_KEY
    os.environ["ZERO_OPENAI_BASE_URL"] = "https://api.justwoker.icu/v1"
    os.environ["ZERO_OPENAI_MODEL"] = MODEL
    os.environ["ZERO_OPENAI_TIMEOUT_SECONDS"] = "180"
    os.environ["ZERO_DECOMPOSITION_ENABLED"] = "1"
    os.environ["ZERO_WORKTREE_ROOT"] = str(REAL_HOME / "worktrees")
    os.environ["ZERO_WORKTREE_ISOLATION_MODE"] = "host_bounded"
    os.environ["ZERO_WORKTREE_ALLOWED_COMMANDS"] = (
        "python3,pip3,ls,cat,git,echo,wc,grep,find,touch,head,tail"
    )
    os.environ["ZERO_EVIDENCE_TEST_COMMAND"] = "python3 -m unittest discover -s tests -v"
    os.environ["ZERO_TELEGRAM_WEBHOOK_SECRET"] = "realrun-webhook-secret-2026"
    # .env (ZERO_SECRET_KEY) must not be shadowed by a stale process env
    os.environ.pop("ZERO_SECRET_KEY", None)
    sys.path.insert(0, "/home/z/my-project/zero/zero-agent-dev-telegram/src")


def load_settings():
    from zero.config import Settings

    return Settings.load(env_file=str(REAL_HOME / ".env"), zero_env_fallback="development")


def build_real_services():
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = load_settings()
    database = Database(settings)
    apply_migrations(database)
    from zero.app.services import build_services

    return settings, build_services(settings, database)


def management_project(services):
    for p in services.identity.list_projects():
        if p.name == "Zero Management":
            return p
    raise RuntimeError("Zero Management project missing — run the wizard first")


def owner_of(services, project):
    return services.identity.get_user(project.owner_user_id)


def record(key: str, value) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if STATE.exists():
        data = json.loads(STATE.read_text(encoding="utf-8"))
    data[key] = value
    STATE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def read_state(key: str, default=None):
    if not STATE.exists():
        return default
    return json.loads(STATE.read_text(encoding="utf-8")).get(key, default)


def notify(services, project, text: str) -> str:
    """REAL outbound message through the binding → bot → Telegram group."""
    binding = read_state("binding_id")
    if not binding:
        raise RuntimeError("binding not seeded")
    from zero.domain.interfaces import InterfaceBindingId

    out = services.interface_transports.send_message(
        project_id=project.id,
        binding_id=InterfaceBindingId(binding),
        actor_id=project.owner_user_id,
        text=text,
    )
    return out
