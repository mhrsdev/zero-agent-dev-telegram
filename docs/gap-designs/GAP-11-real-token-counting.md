# GAP 11 Design — Real Token Counting

Status: design accepted · Phase 1 (first implementation milestone)

## Problem

Every token gate in the codebase uses `estimate_tokens()` from
`src/zero/domain/context.py` (`len(text.encode("utf-8")) // 4`). The
heuristic under-counts CJK and code-heavy text and over-counts English,
which makes compaction thresholds (`CompactionService.should_compact`)
and context budgets (`ContextBuilder.build_context`) drift from real
provider pressure.

## Architecture

A single new module, `src/zero/manage/core/tokenizer.py`, owns all
token counting. It follows Hermes' lesson (tiktoken only where exact
counts matter, cheap heuristic elsewhere) with one improvement: a
single seam that every caller already passes through.

```
count_tokens(text, model=None) -> int
    ├─ tiktoken installed AND model maps to a known encoding → exact count
    └─ otherwise → bytes÷4 heuristic (current behavior, unchanged)
```

- `encoding_for_model(model: str) -> str | None` — model→encoding map
  (`cl100k_base`: gpt-4/gpt-3.5 family; `o200k_base`: gpt-4o/o1/o3
  family). Claude models have no public tiktoken encoding: they return
  `None` and keep the heuristic.
- Encoding objects are cached at module level in
  `_ENCODING_CACHE: dict[str, Any]`. `tiktoken.get_encoding` is
  expensive; it must run once per process per encoding.
- Import of `tiktoken` is lazy and guarded (`_load_tiktoken()` returns
  `None` when absent) so the `[tokenizer]` extra stays optional.
- A module-level toggle `_TIKTOKEN_DISABLED` (settable only via
  `disable_tiktoken_for_testing()`) lets deterministic tests force the
  fallback without uninstalling anything.

### Threading through existing callers

| Caller | File | Change |
|---|---|---|
| `estimate_tokens(text)` | `src/zero/domain/context.py` | keeps signature; internally delegates to `count_tokens(text, None)` — no model ⇒ heuristic only, so **no behavioral change** for callers without a model. |
| NEW `estimate_tokens_for_model(text, model)` | `src/zero/manage/core/tokenizer.py` | used where a model name is available. |
| Compaction threshold check | `CompactionService.should_compact` / `compact` | accepts optional `model_name`; when provided, counts summary/context text via `estimate_tokens_for_model`. |
| Context builder budget | `retrieval_service.ContextBuilder.build_context` | candidate scoring gains an optional `model_name` parameter defaulting to `None`. |
| Usage cost estimation | `provider_service._record_usage` | unchanged — server-reported usage remains authoritative for billing (per `zero-claude-token-economics`: estimates never override server usage). |

The domain layer must not import from `manage/`; therefore
`domain/context.py` receives the delegation through a tiny local hook:
`estimate_tokens` calls `count_tokens` imported lazily inside the
function body with `try/except ImportError` fallback to the pure
arithmetic. This preserves the dependency direction
(`manage → domain`) while keeping one counting seam.

## Data model changes

None.

## API surface

```python
def count_tokens(text: str, model: str | None = None) -> int
def estimate_tokens_for_model(text: str, model: str) -> int
def encoding_for_model(model: str) -> str | None   # "cl100k_base" | "o200k_base" | None
def tiktoken_available() -> bool
```

No HTTP surface changes.

## Security considerations

- No network access: tiktoken may attempt to download BPE files on
  first use for unknown encodings. We only call `get_encoding` for
  encodings in our fixed allow-list, which ship in the wheel's cache;
  if loading fails (offline), we fall back to the heuristic and log at
  debug level. No exception escapes.
- Token counts never include secrets beyond what callers already pass;
  no new logging of message content.

## Test strategy

- Unit tests with tiktoken present: known strings ("hello world",
  empty string, CJK) count exactly vs `tiktoken.get_encoding(...)` on
  a sample model from each family.
- Fallback tests: `disable_tiktoken_for_testing()` forces bytes÷4 and
  matches `estimate_tokens` exactly.
- Model mapping table test: every entry resolves to its documented
  encoding; unknown models return None.
- Cache test: repeated calls return identical values and hit the same
  cached object identity.
- Whole-suite invariant: no existing test installs/uses tiktoken paths
  unless explicitly opted in, so baseline behavior is unchanged.

## Migration path

Additive only. New module + optional extra `[tokenizer]`
(`tiktoken>=0.8`). Callers adopt `estimate_tokens_for_model`
opportunistically where a model name is in scope.

## Rollback strategy

Delete the module and revert caller changes; the heuristic path is the
same code as today, so behavior reverts bit-for-bit.

## Acceptance criteria

- With tiktoken installed: GPT-family models produce exact counts.
- Without tiktoken (or for Claude/unknown models): current bytes÷4
  behavior, documented as approximate.
- Full suite green; ruff/format/compileall clean.
