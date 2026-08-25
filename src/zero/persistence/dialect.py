"""SQLite→PostgreSQL dialect translation (GAP 2).

The repositories embed SQLite-flavoured SQL. Rather than rewriting 13
repository modules, the Postgres connection adapter translates the
bounded, uniform dialect surface at execution time and the migration
generator produces PostgreSQL schema files from the canonical SQLite
ones. Every substitution here is unit-tested; anything outside this
surface must be added explicitly, never guessed.
"""

from __future__ import annotations

import re

#: The canonical timestamp format shared by both dialects.
_PG_TS_FORMAT = '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\''
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_PG_NOW = f"to_char(clock_timestamp(), {_PG_TS_FORMAT})"

_JULIANDAY_LEASE_RE = re.compile(r"julianday\(\s*(?P<col>\w+)\s*\)\s*>\s*julianday\('now'\)")

#: strftime with an optional '+N seconds' modifier applied to either
#: 'now' or a named column.
_STRFTIME_RE = re.compile(
    r"strftime\(\s*'%Y-%m-%dT%H:%M:%fZ'\s*,\s*"
    r"(?:(?P<now>'now')|(?P<col>[\w.]+))"
    r"(?:\s*,\s*'\+(?P<secs>\d+)\s+seconds'\s*)?\)"
)

_TRIGGER_HEADER_RE = re.compile(
    r"CREATE TRIGGER\s+(?:(?P<ifnot>IF NOT EXISTS)\s+)?(?P<name>\w+)\s*",
)
_TRIGGER_ACTION_RE = re.compile(
    r"(?P<when>BEFORE|AFTER|INSTEAD OF)\s+"
    r"(?P<event>UPDATE(?:\s+OF\s+[\w,\s]+?)?|INSERT|DELETE)\s+ON\s+"
    r"(?P<table>\w+)",
    re.IGNORECASE,
)
_TRIGGER_RAISE_RE = re.compile(r"SELECT RAISE\(ABORT,\s*'(?P<message>[^']*)'\);")

_AUTOINCREMENT_RE = re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT")


def translate_dml(sql: str) -> str:
    """Translate a DML/DDL statement from SQLite to PostgreSQL syntax.

    Handles exactly the idioms this codebase uses:
    - ``?`` placeholders (outside string literals) → ``%s``;
    - ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` → an identical-format
      ``to_char(clock_timestamp() ...)`` expression;
    - ``julianday(col) > julianday('now')`` lease comparisons →
      lexicographic text comparison against the same-format now;
    - ``PRAGMA`` statements are dropped (PostgreSQL enforces foreign
      keys natively).

    ``ON CONFLICT ... DO NOTHING/UPDATE`` is already valid PostgreSQL.
    """
    # Pass 1: whole-text idioms whose canonical forms include quoted
    # literals (so they cannot be matched inside per-segment scanning).

    def _strftime_to_pg(match: re.Match) -> str:
        secs = match.group("secs")
        if match.group("now"):
            if secs:
                return f"to_char(clock_timestamp() + interval '{secs} seconds', {_PG_TS_FORMAT})"
            return f"to_char(clock_timestamp(), {_PG_TS_FORMAT})"
        col = match.group("col")
        if secs:
            return (
                f"to_char(to_timestamp({col}, {_PG_TS_FORMAT}) "
                f"+ interval '{secs} seconds', {_PG_TS_FORMAT})"
            )
        return f"to_char(to_timestamp({col}, {_PG_TS_FORMAT}), {_PG_TS_FORMAT})"

    out = _STRFTIME_RE.sub(_strftime_to_pg, sql)
    out = _JULIANDAY_LEASE_RE.sub(lambda m: f"{m.group('col')} > {_PG_NOW}", out)
    # Pass 2: per-segment transforms that must respect string literals.
    return _substitute_outside_strings(out, _translate_segment)


def _translate_segment(segment: str) -> str:
    out = re.sub(
        r"^\s*PRAGMA[^;]*;?\s*$",
        "",
        segment,
        flags=re.MULTILINE,
    )
    out = out.replace("?", "%s")
    return out


def _substitute_outside_strings(sql: str, transformer) -> str:
    """Apply the transformer only to segments outside single quotes."""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            parts.append(("sql", "".join(buf)))
            buf = []
            lit = ["'"]
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        lit.append("''")
                        i += 2
                        continue
                    lit.append("'")
                    i += 1
                    break
                lit.append(sql[i])
                i += 1
            parts.append(("str", "".join(lit)))
            continue
        buf.append(ch)
        i += 1
    parts.append(("sql", "".join(buf)))
    out: list[str] = []
    for kind, chunk in parts:
        out.append(chunk if kind == "str" else transformer(chunk))
    return "".join(out)


def translate_schema(sql: str) -> str:
    """Translate one migration file's SQL from SQLite to PostgreSQL.

    On top of :func:`translate_dml`:
    - ``INTEGER PRIMARY KEY AUTOINCREMENT`` → ``BIGSERIAL PRIMARY KEY``;
    - RAISE(ABORT) guard triggers become plpgsql functions +
      ``EXECUTE FUNCTION`` triggers with identical names and semantics.
    """
    base, trigger_blocks = _extract_raise_triggers(sql)
    out = translate_dml(base)
    out = _AUTOINCREMENT_RE.sub("BIGSERIAL PRIMARY KEY", out)
    for block in trigger_blocks:
        out += "\n" + block + "\n"
    return out


def _extract_raise_triggers(sql: str) -> tuple[str, list[str]]:
    """Pull RAISE-guard triggers out; return (rest, pg_trigger_statements)."""
    lines = sql.splitlines()
    passthrough: list[str] = []
    rendered: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if re.match(r"\s*CREATE TRIGGER\b", lines[i], re.IGNORECASE):
            j = i + 1
            while j < n and not lines[j].strip().upper().endswith("END;"):
                j += 1
            block_text = "\n".join(lines[i : min(j + 1, n)])
            pg_block = _render_pg_guard_trigger(block_text)
            if pg_block is not None:
                rendered.append(pg_block)
                i = j + 1
                continue
            # Unknown trigger shape: keep verbatim (fails loudly in PG).
        passthrough.append(lines[i])
        i += 1
    return "\n".join(passthrough), rendered


def _render_pg_guard_trigger(block: str) -> str | None:
    name_match = _TRIGGER_HEADER_RE.search(block)
    action_match = _TRIGGER_ACTION_RE.search(block)
    raise_match = _TRIGGER_RAISE_RE.search(block)
    if not (name_match and action_match and raise_match):
        return None
    name = name_match.group("name")
    # PostgreSQL has no CREATE TRIGGER IF NOT EXISTS; DROP IF EXISTS
    # makes reruns safe instead.
    when = action_match.group("when")
    event = " ".join(action_match.group("event").split())
    table = action_match.group("table")
    message = raise_match.group("message").replace("'", "''")
    when_cond = ""
    when_search = re.search(r"FOR EACH ROW\s*(.*?)\s*BEGIN", block, re.IGNORECASE | re.DOTALL)
    if when_search is not None and when_search.group(1).strip():
        cond = " ".join(when_search.group(1).split())
        if cond.upper().startswith("WHEN"):
            when_cond = f" WHEN {cond[4:].strip()}"
    function_name = f"zero_{name.lower()}_fn"
    return (
        f"CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $zero$\n"
        "BEGIN\n"
        f"    RAISE EXCEPTION '{message}';\n"
        "END;\n"
        "$zero$ LANGUAGE plpgsql;\n"
        f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
        f"CREATE TRIGGER {name}\n"
        f"    {when} {event} ON {table}\n"
        f"    FOR EACH ROW{when_cond}"
        f" EXECUTE FUNCTION {function_name}();"
    )


def statement_is_idempotent_error(error_message: str) -> bool:
    """True when a PostgreSQL error means 'already exists' (rerun-safe)."""
    lowered = error_message.lower()
    return (
        "already exists" in lowered
        or "duplicate column" in lowered
        or "duplicate object" in lowered
        or "duplicate table" in lowered
    )


__all__ = [
    "statement_is_idempotent_error",
    "translate_dml",
    "translate_schema",
]
