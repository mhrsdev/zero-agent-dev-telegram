# 08 — TUI Information Architecture (Textual)

Full-screen, keyboard-first, SSH-friendly. One dependency: Textual (Python,
maintained; built-in NO_COLOR handling, responsive layouts). ASCII fallbacks
for every glyph; status = text+symbol+color (never color alone).

## Global

- Header tabs mirror nav; footer shows context keys.
- Keys: `1..9,0` jump sections · `/` search/filter · `r` refresh ·
  `?` shortcuts · `q` quit · `Ctrl+C` cancel dialog · `Enter` select ·
  `Esc` back · `d` details drawer.
- Dangerous actions open confirm modal typing verb (`type DELETE`).
- Secrets render `tok_…last4` + `[reveal]` (explicit, per-view, auto-hide 10s).
- Persian/Arabic: Textual handles bidi passably; technical tokens wrapped in
  LTR-isolating spans (`\u2066…\u2069`); fonts degrade to ASCII box drawing.

## Screens (nav order)

1. **Overview** — service state (running/stopped/degraded + since), telegram
   connection (mode/bot username/poll-or-webhook), groups count enabled,
   provider health rows (provider/model/state/last-check), requests today,
   tokens today, est. cost today (labeled estimate), recent errors (5),
   version+channel+update-available, cpu/ram/disk bars, last backup age.
2. **Telegram** — bot identity card, connection mode toggle info, webhook vs
   polling state, token rotate action, test-message sender, event log tail.
3. **Groups** — table: title/chat-id/kind/enabled/agent/features/limits;
   actions add(discover wizard)/enable/disable/edit limits/remove; per-group
   drilldown shows members policy + recent denials with reason codes.
4. **Providers** — cards per provider: protocol/base-url/key(masked)+rotate/
   models chips/health/fallback priority; add via embedded wizard steps;
   `T` run test-completion now.
5. **Models & Routing** — primary selector, fallback ordering (move up/down),
   breaker states w/ manual reset, timeout/attempts fields.
6. **Agents** — list agent types, per-group defaults matrix, feature gates.
7. **Usage** — today/7d/30d pivot (group/provider/model/agent/status);
   token+cost columns (estimate tag); soft/hard limit bars; CSV export path.
8. **Logs** — streaming tail (pause `space`, filter `/`, level mask,
   export `e`); always redacted view.
9. **System** — service controls (start/stop/restart), autostart toggle,
   ports, config paths, env-override report ("managed elsewhere" list).
10. **Backups** — create now, schedule/retention form, archive table
    (size/sha/verified), restore flow with preview + stage mode.
11. **Updates** — current/channel switch, check now, release notes pane,
    apply (runs preflight→backup→apply→health→rollback-on-fail job inline).
12. **Settings** — privacy/telemetry, GUI bind address (+0.0.0.0 warning),
    locale, editor for non-secret config fields (validated live).
13. **Diagnostics** — `zero doctor` rendered as checklist with per-item
    fix buttons where safe; JSON copy; support-bundle builder with file
    preview + secret-scan result before export.

## Responsive rules

≥100 cols full tables; 80–99 collapse optional columns into detail drawer;
<80 switches to stacked cards. All async ops show spinner + cancellable
state; SSH drop → Textual suspend/restore preserves screen.
