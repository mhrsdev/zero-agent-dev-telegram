# 09 — Local Web GUI Information Architecture

Server-rendered Jinja2 + htmx (one ~10KB vendored js file), mounted by the
same FastAPI app at `/admin`. Shares SetupService/etc. with TUI/CLI. Visual
language: black/white minimal, light+dark via `prefers-color-scheme` +
toggle; system font stack; no animations beyond 120ms fades.

## Security defaults (non-negotiable)

- Bind `127.0.0.1:PORT` (default 8787). Changing host requires editing
  config + explicit `access.gui_bind_warning_acknowledged_at`; banner +
  docs push SSH tunneling (`ssh -L 8787:127.0.0.1:8787 host`).
- First run: one-time **setup token** printed by installer/CLI
  (`zero status --json` shows it), valid 15 min / single use → forces admin
  password creation (scrypt hash stored in `admin_users`; plaintext never).
- Sessions: signed httponly samesite=strict cookie, 30-min idle + 12-h abs
  expiry, server-side revocation list; login rate limit 5/min/IP with
  exponential lockout; CSRF token per session on every mutating form/htmx
  call.
- Security headers: CSP (self only), X-Frame-Options deny, Referrer-Policy,
  nosniff. All API responses redact secrets (refs only); audit every admin
  mutation (`operation=admin.<action>`).

## Pages

| Route | Page | Contents/actions |
|---|---|---|
| /admin/login | Login | setup-token first-run → set password |
| /admin | Dashboard | same cards as TUI Overview (status, telegram, groups, providers health, today usage/cost estimate, errors, version/update, host stats, last backup) |
| /admin/wizard | Setup Wizard | step list w/ progress; per-step form identical to state machine; save-draft/resume buttons |
| /admin/telegram | Bot | identity card, mode info, rotate token, send test message, recent events table |
| /admin/groups | Groups & Access | table + add(discover flow)/edit policy modal(mode, features, limits)/enable-disable/remove; denial reasons viewer |
| /admin/providers | Providers | cards: protocol/base-url/masked key+rotate/models chips/health/priority; Add-provider multi-step form; Test button |
| /admin/routing | Models & Routing | primary select, fallback order, breaker states + reset, timeouts/attempts |
| /admin/agents | Agents | agent types, group-default matrix, feature toggles |
| /admin/usage | Usage & Cost | filters (range/group/provider/model/agent/status), totals + per-bucket tables, limit editors (soft/hard), CSV export; "estimates" label everywhere cost appears |
| /admin/logs | Logs | tail w/ level filter/search, download redacted |
| /admin/system | System Health | service controls, ports, paths, env-override report, disk/mem |
| /admin/backups | Backup & Restore | create/schedule/retention; archive table(verify sha); restore wizard: preview→stage→commit |
| /admin/updates | Updates | channel, check, notes, apply job progress, rollback button |
| /admin/security | Security | sessions list+revoke-all, admin password change, GUI bind warning state, audit excerpt of admin actions |
| /admin/settings | Settings | non-secret config fields w/ live validation; diff-before-save view |

## Non-goals

No multi-tenancy, no billing, no team management, no public exposure story —
that is Zero Dev Web's territory. Single-instance local administration only.
