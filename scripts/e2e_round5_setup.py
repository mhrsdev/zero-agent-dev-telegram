"""Round-5 E2E setup (real credentials) — run ONCE before booting.

Creates $ZERO_HOME with:
- a pinned sqlite database + migrations,
- the management project/owner,
- the REAL Telegram bot token + REAL LLM api key stored via the secret
  store (sec_ references, never plaintext),
- config.yaml wiring the provider (api.justwoker.icu/v1, claude-opus-5),
  the bot token ref, owner_only access, and the REAL group
  -1004406039396 as an enabled binding scope.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path("/home/z/my-project/zero-e2e-home")
DB_PATH = HOME / "e2e.db"
SECRET_KEY = "e" * 64

import os as _os
PROVIDER_BASE = _os.environ.get("E2E_PROVIDER_BASE", "https://api.justwoker.icu/v1")
PROVIDER_MODEL = "claude-opus-5"
GROUP_ID = "-1004406039396"
# Real credentials are injected per run — never committed. The literal
# values that used to live here were live secrets (scrubbed 2026-08-30).
PROVIDER_KEY = _os.environ.get("E2E_PROVIDER_KEY", "").strip()
BOT_TOKEN = _os.environ.get("E2E_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = _os.environ.get("E2E_WEBHOOK_SECRET", "").strip()


def main() -> int:
    import shutil

    missing = [
        name
        for name, value in (
            ("E2E_PROVIDER_KEY", PROVIDER_KEY),
            ("E2E_BOT_TOKEN", BOT_TOKEN),
            ("E2E_WEBHOOK_SECRET", WEBHOOK_SECRET),
        )
        if not value
    ]
    if missing:
        sys.exit(
            "missing required environment variables: "
            + ", ".join(missing)
            + " (real credentials are no longer embedded in this script)"
        )

    if HOME.exists():
        shutil.rmtree(HOME)
    HOME.mkdir(parents=True)
    (HOME / ".env").write_text(
        "ZERO_ENV=development\n"
        f"ZERO_DATABASE_URL=sqlite:///{DB_PATH}\n"
        f"ZERO_SECRET_KEY={SECRET_KEY}\n"
        f"ZERO_TELEGRAM_WEBHOOK_SECRET={WEBHOOK_SECRET}\n",
        encoding="utf-8",
    )

    os.environ["ZERO_HOME"] = str(HOME)
    os.environ["ZERO_ENV"] = "development"
    os.environ["ZERO_DATABASE_URL"] = f"sqlite:///{DB_PATH}"
    os.environ["ZERO_SECRET_KEY"] = SECRET_KEY

    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.config import Settings

    settings = Settings.load(env_file=str(HOME / ".env"), zero_env_fallback="development")
    database = open_database(settings)
    apply_migrations(database)

    from zero.app.services import build_services

    services = build_services(settings, database)

    from zero.manage.cli import _ensure_management_scope

    project = _ensure_management_scope(services)
    owner = project.owner_user_id

    bot_ref = services.secrets.store(
        project_id=project.id,
        name="telegram-bot-token",
        secret_type="token",
        value=BOT_TOKEN,
        actor_id=owner,
    )
    key_ref = services.secrets.store(
        project_id=project.id,
        name="primary-llm-key",
        secret_type="api_key",
        value=PROVIDER_KEY,
        actor_id=owner,
    )

    from zero.manage.core.config import (
        ConfigService,
        GroupPolicy,
        ProviderCfg,
        RoutingCfg,
    )

    cfgsvc = ConfigService(HOME)
    cfg = cfgsvc.load()
    cfg.owner_project_id = project.id.value
    cfg.telegram.bot_token_ref = bot_ref.id.value
    cfg.providers = [
        ProviderCfg(
            id="primary-llm",
            protocol="openai_compatible",
            base_url=PROVIDER_BASE,
            api_key_ref=key_ref.id.value,
            models=[PROVIDER_MODEL],
        )
    ]
    cfg.routing = RoutingCfg(primary_model=PROVIDER_MODEL)
    cfg.access = cfg.access.__class__(mode="owner_only")
    cfg.access.groups = [
        GroupPolicy(
            chat_id=GROUP_ID,
            title="Zero E2E Group",
            kind="supergroup",
            enabled=True,
        )
    ]
    cfgsvc.save(cfg)

    # Pre-link the operator's Telegram identity through the REAL
    # identity pipeline (verified=True). The auto-link-owner bootstrap
    # is a one-shot convenience: whoever sends the FIRST message to the
    # bot becomes the owner. On a live bot the real group delivers real
    # messages around the clock — during this e2e a real member's
    # message raced the driver and consumed the bootstrap, leaving the
    # driver's synthetic sender permanently unlinked. Deterministic
    # e2e requires the deterministic link.
    services.identity.link_external_identity(
        user_id=owner,
        platform="telegram",
        external_id="8478981617",
        external_username="e2e_owner",
        verified=True,
    )

    # NOTE (round-7): NO repository is registered on the e2e project.
    # Registering one makes EVERY task workspace-bound (repository_id is
    # not None → worktree creation → git commands), and command execution
    # is architecturally unavailable in this sandbox (GAP-3 fail-closed:
    # no docker/firejail isolation backend). With no repository, tasks
    # carrying provider_response evidence run the real agent loop while
    # file-editing tasks fail closed — the honest environmental boundary.

    print(
        json.dumps(
            {
                "home": str(HOME),
                "db": str(DB_PATH),
                "project_id": project.id.value,
                "bot_ref": bot_ref.id.value,
                "key_ref": key_ref.id.value,
                "webhook_secret": WEBHOOK_SECRET,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
