# 11 — Migration Plan (existing installs → managed)

## Personas

A) **Fresh user** (target persona): nothing installed.
B) **Existing dev** (clone + venv + env exports).
C) **Existing server-ish** (clone + env file + manual process).

## Path A — fresh
`install.sh` → dedicated `zero` user (native) or compose project → venv at
`/opt/zero` → `zero.service` enabled → `zero setup` wizard → done. No
migration needed; engine DB created by `migrate` on first start.

## Path B — existing clone/venv
1. `pip install -e .` inside their existing venv (new `zero` script appears).
2. `zero setup --from-env` importer: reads current `ZERO_*` from environment/
   shell profile hint, writes `config.yaml` v1, creates secret refs for any
   inline provider keys by storing them into the Fernet store and replacing
   config values with refs. Engine env behavior unchanged (env still wins),
   so nothing breaks if they keep exporting.
3. Optional adoption of service management: `zero install --adopt`
   generates unit pointing at THEIR venv path (no move required).

Rollback: delete generated `config.yaml` (+refs left in store are inert);
engine env-only behavior restored.

## Path C — existing env-file servers
Same as B plus: unit template uses `EnvironmentFile=` for any vars they keep
outside config; ConfigService marks those keys "managed elsewhere" so UIs
display but don't edit them.

## Data migrations

- Engine schema: one new migration set `0029_management.sql`
  (provider_health, group_policies, usage_counters, admin_users,
  setup_tokens). Additive only — no column changes to existing tables;
  double-run idempotency covered by existing runner tests.
- Config file: none existed before; importer is additive. Schema upgrades
  handled by core/config MIGRATIONS with pre-copy backup.

## Compatibility guarantees

- `zero-develop …` commands/flags untouched (CI release smoke continues to
  pass unmodified).
- Env-var contract remains authoritative when present (documented precedence:
  env > config.yaml > defaults). Wizard surfaces overrides instead of fighting them.
- Telegram runtime protocols unchanged; new policy gate inserts before
  identity resolution and defaults to permissive-today semantics
  (`owner_only` enforced as: allow if sender == project owner's linked
  identity; otherwise previous rules apply until owner edits policy) — no
  existing working group breaks on upgrade.

## Rollback strategy (global)

Every milestone ships behind the branch; tags cut per milestone
(`mgmt-m1`…). Native installs keep previous venv directory (`/opt/zero@prev`)
+ DB backup taken by update flow; `zero update --rollback-to <tag>` restores
symlink + replays last-good config. Uninstall separates app vs data
explicitly.

## Deprecations

None forced in v1. `.env.example` gains a header pointing to the wizard;
README quick-start replaced by 60-second path while manual path moves to
docs/manual-install.md.
