# 07 — CLI Design (`zero`)

New console script `zero = zero.manage.cli:main` alongside `zero-develop`
(engine entry kept for compatibility/CI). Built on argparse subparsers; every
command supports `--json` (machine-readable) and `--config PATH`.
Non-interactive flags enable full automation; interactive prompts only when
stdin is a TTY and required input missing.

## Commands

| Command | Purpose | Key flags |
|---|---|---|
| `zero install` | one-command install (thin client over scripts/install.sh logic; on Linux delegates to it; validates post-conditions) | `--mode native\|docker`, `--unattended`, `--channel stable\|beta`, `--no-wizard`, `--base-url URL`, `--user NAME` |
| `zero setup` | wizard (auto-resumes draft) | `--resume`, `--non-interactive --step id=value …`, `--from-env` (import ZERO_*), `--dry-run` |
| `zero start/stop/restart/status` | service control via systemd/compose adapters | `--follow` (status), `--json` |
| `zero logs [-f] [--since …] [--grep …] [--level …]` | journalctl/docker logs wrapper + secret redaction filter always-on | |
| `zero doctor` | diagnostics (doc 14 spec) | `--json`, `--fix <issue-id>` (asks confirm unless `--yes`), `--bundle OUT.tar.gz` (preview with `--list-bundle`) |
| `zero update [check\|apply]` | channel-aware update: preflight→backup→apply→health→auto-rollback | `--channel`, `--rollback-to TAG` |
| `zero backup [create\|list\|verify FILE]` | wraps BackupService | `--include-secrets`(confirm), `--dest DIR`, `--retention N` |
| `zero restore FILE` | preview → stage → commit | `--stage-only`, `--force`, `--confirm-hash H` |
| `zero telegram add-bot` | masked token input, getMe verify, store ref | `--token-file -` (read stdin, never argv/history) |
| `zero telegram groups discover\|add\|list\|enable\|disable\|remove` | group discovery via updates probe; stores verified policies | `--chat-id`, `--title`, `--default-agent`, `--yes` |
| `zero access set-mode owner_only\|users\|groups\|users_and_groups\|public` | policy gate; `public` requires `--i-understand-public` | |
| `zero providers add/list/test/remove` | provider wizard non-interactive form | `--protocol --base-url --key-file - --models a,b --priority N`; `test` runs auth+completion probes |
| `zero models primary M [--fallback a,b]` | routing assignment validated against catalog | |
| `zero agents list/set-default GROUP AGENT` | per-group agent/features | |
| `zero usage summary [--today\|--days N] [--group --provider --model --agent --status] [--json]` | aggregates from counters (doc: usage) | `--csv` |
| `zero limits set --daily-tokens N [--group ID …][--soft N]` | budgets incl. soft warn | |
| `zero config show\|edit\|diff\|validate\|export [--redact]\|rollback` | last-known-good ops; `show` redacts by default | |
| `zero secrets set NAME (--key-file -)\|rotate NAME\|delete NAME` | masked I/O; values never argv/env/echo | |
| `zero websearch enable/disable/status` | gated by provider presence | |
| `zero uninstall` | double-confirm; distinguishes app vs data; optional backup first | `--keep-data`\|`--purge-data`, `--remove-user` |

## Cross-cutting rules

- **Secrets:** accepted only via stdin/file/hidden prompt; never argv/env;
  prompt uses getpass; process listings can't leak; logs pass through
  `redact_sensitive_text`.
- **Exit codes:** `0 ok · 1 operation failed · 2 usage/config error ·
  3 needs-confirmation-missing · 4 unhealthy(doctor/update preflight)`.
- **Automation example:**

```bash
curl -fsSL https://getzerodev.ai/install.sh | sh -s -- --unattended \
  --channel stable --mode native --no-wizard
printf '%s' "$BOT_TOKEN"   | zero telegram add-bot --token-file -
printf '%s' "$OPENAI_KEY"  | zero providers add --id openai-primary \
    --protocol openai_compatible \
    --base-url https://api.openai.com/v1 --key-file - --models gpt-4o-mini
zero access set-mode groups
zero telegram groups add --chat-id -1001234567890 --title "Apollo Dev" --yes
zero setup --non-interactive --step privacy.telemetry=false --step updates.channel=stable
zero doctor && zero start && zero status --json
```

## Implementation notes

Thin dispatch layer only: parses args → calls SetupService /
TelegramAdminService / etc. No business logic, no direct file/DB writes.
All long operations print phase lines from the real progress callbacks
(branding module subscribes; no fake progress).
