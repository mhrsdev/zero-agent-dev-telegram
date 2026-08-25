"""Generate PostgreSQL migration files from the canonical SQLite ones (GAP 2).

Usage::

    python scripts/gen_pg_migrations.py

Reads ``src/zero/persistence/migrations/*.sql`` and writes dialect-
translated copies to ``src/zero/persistence/migrations_pg/*.sql`` using
:func:`zero.persistence.dialect.translate_schema`. The outputs are
committed: runtime never generates SQL. Re-run after adding a SQLite
migration, then review the diff.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_MIGRATIONS = REPO_ROOT / "src" / "zero" / "persistence" / "migrations"
OUT_MIGRATIONS = REPO_ROOT / "src" / "zero" / "persistence" / "migrations_pg"


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from zero.persistence.dialect import translate_schema

    OUT_MIGRATIONS.mkdir(parents=True, exist_ok=True)
    count = 0
    for source in sorted(SRC_MIGRATIONS.glob("*.sql")):
        translated = translate_schema(source.read_text(encoding="utf-8"))
        header = (
            f"-- GENERATED from {source.name} by scripts/gen_pg_migrations.py.\n"
            "-- PostgreSQL dialect translation of the canonical SQLite schema.\n"
            "-- Do not edit directly; re-run the generator instead.\n\n"
        )
        (OUT_MIGRATIONS / source.name).write_text(header + translated, encoding="utf-8")
        count += 1
    print(f"wrote {count} translated migrations to {OUT_MIGRATIONS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
