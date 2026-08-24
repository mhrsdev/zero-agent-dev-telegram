# 10 — Threat Model (STRIDE, product-scoped)

Assets: bot token, provider API keys, Telegram chat ids/policy, admin
password/session, config file, SQLite DB (usage/audit), host shell reach
(worktree commands), reputation (public-bot accident).

| # | Threat | Vector | Controls (existing → new) |
|---|--------|--------|---------------------------|
| S1 | Spoofed Telegram update triggers LLM/tools | forged webhook | ✅ signature verify before binding lookup; NEW: policy gate pre-LLM + generic denial text; replay window via update_id dedupe (exists) |
| S2 | Spoofed admin | GUI access from LAN | NEW: loopback default, setup-token bootstrap, scrypt password, session hardening, rate limit, CSRF, audit |
| T1 | Token/key tampering in config | local file write | NEW: 0600 perms, atomic replace+fsync, last-good + diff view; refs keep values out of file |
| T2 | MITM provider calls | network | ✅ https enforced by URL validation; pin docs for proxies |
| R1 | Secret disclosure in logs/diagnostics/exports | verbose errors, bundles | ✅ redact formatter+canary scan; NEW: export redact-by-default, bundle preview+scan gate, masked inputs everywhere |
| R2 | Public bot accident → cost blowout | binding on big group | NEW: owner_only default; public requires typed confirmation; per-group budgets + soft/hard stops pre-request |
| I1 | Prompt/response leakage via usage store | over-collection | NEW: counters only (ids+tokens+cost), never bodies; documented invariant + test asserting no message content columns written |
| I2 | Config leaks via `/admin` JSON | naive serializer | NEW: pydantic `exclude` refs; golden test: no `sec_` values, no key material in any admin response snapshot |
| D1 | DoS by group member spam | flood | NEW per-group rate limits + global daily budget checked before provider dispatch (429-style polite reply) |
| D2 | Restart loop from bad creds | invalid token at boot | NEW: doctor preflight + engine keeps last-good config; service unit `Restart=on-failure` with `StartLimitIntervalSec`; wizard validates before commit |
| E1 | Privilege escalation via command allowlist abuse (`rm /`, device writes) | run_command | ✅ bare-name allowlist, scrubbed env, argv (no shell); NEW hardline floor patterns; non-force cleanup already |
| E2 | Path traversal in backup restore entries | malicious archive | ✅ staging DB verify; NEW zip-slip path check + manifest hash before stage |
| E3 | CSRF/XSS on GUI | crafted link/page | NEW: CSRF token, CSP self-only, Jinja autoescape, no inline js |

Residual risks accepted for v1: single-host SQLite concurrency envelope;
physical/host-user compromise (out of scope); user-session mode absent
(removes that whole attack class this release).
