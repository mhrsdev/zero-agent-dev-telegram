# ADR 0008 — Encrypted Secret Storage with Server-Side Resolution

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 3 (Authorization, Secret Boundary, Tool Registry, Audit Core)
- Skills applied: `zero-control-plane-trust`, `zero-tool-capability-runtime`,
  `zero-claude-token-economics`

## Context

`PLAN.md` §8 (Milestone 3) requires:
- Raw secrets never enter model context or ordinary logs.
- Server-side secret reference/lookup boundary.

`zero-control-plane-trust` §"Secrets are usable without being
visible": "A secret reference and a secret value are different kinds
of data. Models, logs, audits, frontend state, and ordinary database
records may carry a reference or capability ID. Only the server-side
integration boundary resolves the raw value at the last responsible
moment."

`zero-tool-capability-runtime` §"Secrets resolve at the last
responsible moment": "The registry stores a secret reference, not a
raw value in model-visible metadata. The server wrapper resolves the
credential immediately before the external call and excludes it from
request summaries, errors, logs, and artifacts."

## Decision

Adopt encrypted secret storage with three layers:

1. **Reference**: a `SecretReference` carries metadata (id, project,
   name, type, created_at, revoked_at) but never the value. This is
   what the application, audit, and model-facing code sees.
2. **Encrypted value**: the raw secret is encrypted with Fernet
   (symmetric authenticated encryption) and stored in the
   `encrypted_value` column of `secret_references`. The encryption
   key is derived from `ZERO_SECRET_KEY` using HKDF-SHA256.
3. **Resolution**: only `SecretService.resolve_value` decrypts the
   value, and only at the tool capability runtime's request. The
   returned string is held in memory for the minimum time necessary
   and is never logged, audited, or returned to a client.

### Encryption details

- **Algorithm**: Fernet (AES-128-CBC + HMAC-SHA256) from the
  `cryptography` package. Fernet provides authenticated encryption:
  tampering with the ciphertext is detected on decryption.
- **Key derivation**: HKDF-SHA256 with a fixed info string
  (`b"zero-develop/secret-encryption/v1"`) derives a 32-byte key
  from `ZERO_SECRET_KEY`. HKDF adds key-separation between the
  application's signing key and the encryption key.
- **Key management**: the encryption key is held in process memory
  only. It is never written to disk, never logged, never returned to
  a client. Rotating `ZERO_SECRET_KEY` requires re-encrypting all
  secrets (a future migration).

### Trust boundary

The only method that returns the raw value is
`SecretService.resolve_value(project_id, secret_id)`. It is called
only by:
- `ToolService.invoke` (passing `secret_service` to the tool context);
- future provider adapters (M10) for resolving provider API keys.

It is NEVER called by:
- HTTP handlers (the secret storage endpoint returns only metadata);
- audit (audit events record the secret ID, never the value);
- logs (no log line contains the raw value);
- model-facing rendering (the tool result's `model_facing` field
  contains only the bounded output, never the secret).

### Revocation

A secret can be revoked (`SecretService.revoke`). Revoked secrets
cannot be resolved; `resolve_value` raises
`SecretRevokedError`. The metadata is preserved for audit; only the
ability to resolve the value is removed.

### Project isolation

Secrets are project-scoped: `secret_references.project_id` is a FK
to `projects`, and all queries filter by `project_id` before content
is loaded. A secret stored in project A cannot be retrieved from
project B by guessing the ID.

## Rejected alternatives

- **Store secrets in plaintext**: explicitly rejected by
  `zero-control-plane-trust` §"Secrets are usable without being
  visible" and by PLAN.md invariant #13.
- **Store secrets in an external vault (HashiCorp Vault, AWS Secrets
  Manager)**: deferred. Adds an external dependency that the current
  vertical slice does not need. The Fernet-based approach is
  sufficient; migration to an external vault is a future
  operational decision.
- **Use the raw `ZERO_SECRET_KEY` directly as the Fernet key**:
  rejected. HKDF adds key-separation, so a future change to
  `ZERO_SECRET_KEY` for signing purposes does not silently break
  secret decryption (and vice versa).
- **Return masked secrets to the client**: rejected. Even masked
  text is delivered secret material (per `zero-web-control-surface`
  §"Secret and sensitive views minimize exposure"). The HTTP
  endpoint returns only metadata.

## Consequences

- Secrets are encrypted at rest. A database backup does not expose
  raw secrets unless `ZERO_SECRET_KEY` is also compromised.
- Rotating `ZERO_SECRET_KEY` requires re-encrypting all secrets. A
  future migration will provide a `rotate_key` operation.
- The trust boundary is in one method (`resolve_value`), making it
  easy to audit and test.
- The capability runtime receives the `SecretService` through the
  `ToolContext`, so handlers can resolve secrets at invocation time
  without the secret appearing in the handler's arguments.
