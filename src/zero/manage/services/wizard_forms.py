"""Presentation-neutral wizard form specs.

Each setup step maps to typed field descriptors so CLI/TUI/GUI render the
same inputs and validation without duplicating logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # text|password|bool|select|int
    required: bool = False
    options: tuple[str, ...] = ()
    default: Any = None
    help: str = ""


@dataclass(frozen=True)
class WizardStep:
    id: str
    title: str
    optional: bool = False
    fields: tuple[Field, ...] = ()
    skippable_to_next: bool = True  # Enter with empty values skips if optional


def _f(name, label, **kw):
    return Field(name=name, label=label, **kw)


WIZARD_STEPS: dict[str, WizardStep] = {
    s.id: s
    for s in (
        WizardStep("welcome", "Welcome & installation summary"),
        WizardStep(
            "environment",
            "Server & environment check",
            fields=(
                _f(
                    "environment",
                    "Environment",
                    kind="select",
                    options=("development", "production"),
                    default="development",
                ),
            ),
        ),
        WizardStep(
            "version",
            "Version channel",
            fields=(
                _f(
                    "channel",
                    "Channel",
                    kind="select",
                    options=("stable", "beta"),
                    default="stable",
                ),
            ),
        ),
        WizardStep(
            "telegram_mode",
            "Telegram connection mode",
            fields=(
                _f(
                    "mode",
                    "Mode",
                    kind="select",
                    options=("bot_api",),
                    default="bot_api",
                    help="User-session mode is intentionally not offered in this release.",
                ),
            ),
        ),
        WizardStep(
            "telegram_credentials",
            "Telegram bot token",
            fields=(
                _f(
                    "token",
                    "Bot token (from @BotFather)",
                    kind="password",
                    required=True,
                    help="Validated via Telegram getMe; stored encrypted.",
                ),
            ),
        ),
        WizardStep(
            "provider_add",
            "AI provider",
            fields=(
                _f("id", "Provider id", required=True, default="openai-primary"),
                _f(
                    "protocol",
                    "Protocol",
                    kind="select",
                    options=("openai_compatible", "anthropic"),
                    default="openai_compatible",
                ),
                _f(
                    "base_url",
                    "Base URL",
                    required=True,
                    default="https://api.openai.com/v1",
                ),
                _f("api_key", "API key", kind="password", required=True),
            ),
        ),
        WizardStep(
            "provider_test",
            "Provider test completion",
            fields=(_f("model", "Model to test", required=True),),
            optional=True,
        ),
        WizardStep(
            "model_assign",
            "Model routing",
            fields=(
                _f("primary_model", "Primary model", required=True),
                _f("fallback_models_csv", "Fallback models (comma separated)"),
            ),
        ),
        WizardStep(
            "access_mode",
            "Access policy",
            fields=(
                _f(
                    "mode",
                    "Who can use the bot?",
                    kind="select",
                    options=("owner_only", "users", "groups", "users_and_groups", "public"),
                    default="owner_only",
                ),
                _f("confirm_public", "Confirm public access", kind="bool"),
            ),
        ),
        WizardStep(
            "groups",
            "Groups",
            fields=(
                _f("chat_id", "Group chat id"),
                _f("title", "Group title"),
                _f("token", "Bot token (for discovery)", kind="password"),
            ),
        ),
        WizardStep(
            "agents",
            "Agents",
            fields=(_f("default_agent", "Default agent", default="main_worker"),),
        ),
        WizardStep(
            "memory_storage",
            "Memory & storage",
            # Audit D5: compaction threshold tuning is NOT wired into the
            # engine yet; collecting it here silently dropped the value.
            # The step stays so operators see the state explicitly.
            fields=(),
            optional=True,
        ),
        WizardStep(
            "websearch",
            "Web search (optional)",
            fields=(
                _f("enabled", "Enable web search", kind="bool"),
                # ZeroConfig requires websearch.provider_id to reference a
                # configured provider; say so up front (validation also
                # enforces it with the available ids).
                _f("provider_id", "Search provider id (must match a configured AI provider id)"),
                _f("api_key", "Search API key", kind="password"),
            ),
            optional=True,
        ),
        WizardStep(
            "privacy",
            "Privacy & telemetry",
            fields=(_f("telemetry_enabled", "Enable telemetry", kind="bool"),),
        ),
        WizardStep(
            "updates",
            "Update policy",
            fields=(
                _f(
                    "channel",
                    "Channel",
                    kind="select",
                    options=("stable", "beta"),
                    default="stable",
                ),
                _f("auto_apply", "Auto-apply updates", kind="bool"),
            ),
        ),
        WizardStep(
            "backup_policy",
            "Backup policy",
            fields=(
                _f(
                    "schedule",
                    "Schedule",
                    kind="select",
                    options=("daily", "hourly", "off"),
                    default="daily",
                ),
                _f("retention", "Retention count", kind="int", default=7),
            ),
        ),
        WizardStep("final_validation", "Final validation"),
        WizardStep(
            "test_message",
            "Send test message",
            optional=True,
            fields=(_f("chat_id", "Chat id for the test message"),),
        ),
    )
}
