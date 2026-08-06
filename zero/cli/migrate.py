"""Zero v2 migrate CLI — ADR T-0.5.

One-way, opt-in migration tool from v1 → v2.

Usage:
    zero-migrate --from <v1-data-dir> --to <v2-config> [--dry-run] [--only <kinds>]

Per ADR 0005 §2 golden rule: NO v1 data auto-becomes mode=dev.
All migrated data goes to mode=normal (or personal for user-attributed).

Per ADR 0005 §3: no fact/decision records created from v1 data.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from zero.core.scope import Scope
from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource


@click.command()
@click.option("--from", "v1_path", type=click.Path(exists=True), required=True, help="v1 data directory")
@click.option("--to", "v2_config", type=click.Path(exists=True), required=True, help="v2 config file")
@click.option("--dry-run", is_flag=True, default=True, help="Dry-run mode (default)")
@click.option("--commit", is_flag=True, default=False, help="Actually write data (default is dry-run)")
@click.option("--only", "kinds", multiple=True, help="Only migrate these kinds (semantic, episodic, ...)")
def main(v1_path: str, v2_config: str, dry_run: bool, commit: bool, kinds: tuple[str, ...]) -> None:
    """Migrate Zero v1 data to v2.

    Per ADR 0005 §2 golden rule: NO v1 data auto-becomes mode=dev.
    All migrated data goes to mode=normal (or personal for user-attributed).

    Per ADR 0005 §3: no fact/decision records created from v1 data.
    """
    if not commit:
        click.echo("[DRY RUN] No data will be written. Use --commit to actually migrate.")
    else:
        click.echo("[COMMIT] Migration will write data.")

    click.echo(f"Source: {v1_path}")
    click.echo(f"Target config: {v2_config}")
    if kinds:
        click.echo(f"Kinds: {kinds}")
    else:
        click.echo("Kinds: all (semantic, episodic, preference)")

    v1_dir = Path(v1_path)
    v2_cfg_path = Path(v2_config)

    # Load v2 config.
    try:
        v2_cfg = yaml.safe_load(v2_cfg_path.read_text())
    except Exception as e:
        click.echo(f"Error loading v2 config: {e}", err=True)
        return

    # Initialize v2 DB + memory store if committing.
    _v2_memory_store = None
    if commit:
        import asyncio  # noqa: PLC0415
        from zero.db import Database  # noqa: PLC0415
        from zero.db.sqlite_backend import SqliteBackend  # noqa: PLC0415
        from zero.memory.db_store import DbMemoryStore  # noqa: PLC0415

        db_dir = v2_cfg.get("database", {}).get("sqlite_dir", "~/.zero/db")
        backend = SqliteBackend(sqlite_dir=Path(db_dir).expanduser())
        db = Database(backend=backend)

        async def _init_db() -> DbMemoryStore:
            await db.start()
            return DbMemoryStore(db)

        try:
            _v2_memory_store = asyncio.run(_init_db())
        except Exception as e:
            click.echo(f"Error initializing v2 database: {e}", err=True)
            return

    # Find v1 memory database.
    v1_db = v1_dir / "zero.db"
    if not v1_db.exists():
        # Try common alternatives.
        v1_db = v1_dir / "data" / "zero.db"
    if not v1_db.exists():
        click.echo(f"Error: v1 database not found at {v1_dir}/zero.db", err=True)
        click.echo("Looked for: zero.db, data/zero.db")
        return

    click.echo(f"Found v1 database: {v1_db}")

    # Connect to v1 database (read-only).
    try:
        v1_conn = sqlite3.connect(str(v1_db))
        v1_conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        click.echo(f"Error opening v1 database: {e}", err=True)
        return

    # Migrate memory entries.
    migrated_count = 0
    skipped_count = 0
    error_count = 0

    # Determine which tables exist in v1.
    cursor = v1_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%memory%'"
    )
    v1_tables = [row[0] for row in cursor.fetchall()]
    click.echo(f"v1 memory tables found: {v1_tables}")

    # Map v1 tables to v2 kinds.
    table_kind_map = {
        "semantic_memory": MemoryKind.SEMANTIC,
        "memory_v3": MemoryKind.SEMANTIC,
        "episodic_memory": MemoryKind.EPISODIC,
        "experience_memory": MemoryKind.EPISODIC,
    }

    for table_name in v1_tables:
        if table_name not in table_kind_map:
            click.echo(f"  Skipping unknown table: {table_name}")
            continue

        kind = table_kind_map[table_name]
        if kinds and kind.value not in kinds:
            continue

        # Per ADR 0005 §3: no fact/decision from v1 data.
        if kind in (MemoryKind.FACT, MemoryKind.DECISION):
            click.echo(f"  Skipping {kind.value} (ADR 0005 §3: no fact/decision from v1)")
            continue

        try:
            rows = v1_conn.execute(
                f"SELECT * FROM {table_name} LIMIT 10000"
            ).fetchall()
        except sqlite3.Error as e:
            click.echo(f"  Error reading {table_name}: {e}", err=True)
            error_count += 1
            continue

        click.echo(f"  Processing {table_name}: {len(rows)} rows")

        for row in rows:
            try:
                # Extract fields (v1 schema varies, so be defensive).
                content = row["content"] if "content" in row.keys() else row["text"] if "text" in row.keys() else ""
                if not content:
                    skipped_count += 1
                    continue

                # Determine scope: user-attributed → personal, group → normal.
                # Per ADR 0005 §2: NEVER mode=dev.
                user_id = row["user_id"] if "user_id" in row.keys() else None
                group_id = row["group_id"] if "group_id" in row.keys() else None

                if user_id and not group_id:
                    # Personal memory.
                    scope = Scope.personal(
                        user_id=str(user_id) if str(user_id).startswith("usr_") else f"usr_v1_{user_id}"
                    ).with_default_memory_scope()
                elif group_id:
                    # Group memory → NORMAL mode (never dev).
                    topic_id = row["topic_id"] if "topic_id" in row.keys() else 0
                    scope = Scope.normal(
                        group_id=str(group_id) if str(group_id).startswith("grp_") else f"grp_v1_{group_id}",
                        topic_id=int(topic_id),
                    ).with_default_memory_scope()
                else:
                    # Ambiguous owner — skip per ADR 0005 §2.
                    skipped_count += 1
                    continue

                # Build source reference.
                v1_id = row["id"] if "id" in row.keys() else f"v1_row_{migrated_count}"
                source = MemorySource(type="import", ref=f"v1:{v1_id}")

                # Create v2 memory entry.
                entry = MemoryEntry(
                    scope=scope,
                    kind=kind,
                    content=str(content),
                    source=source,
                    created_by="zero_migrate",
                    # No approved_by — v1 data has no human approval.
                )

                if commit:
                    # Write to v2 DB via DbMemoryStore.
                    if _v2_memory_store is not None:
                        try:
                            import asyncio  # noqa: PLC0415
                            asyncio.run(_v2_memory_store.store(entry))
                        except Exception as write_err:
                            error_count += 1
                            if error_count <= 5:
                                click.echo(f"    Write error: {write_err}", err=True)
                            continue

                migrated_count += 1

            except Exception as e:
                error_count += 1
                if error_count <= 5:
                    click.echo(f"    Error on row: {e}", err=True)

    v1_conn.close()

    click.echo()
    click.echo("=== Migration Summary ===")
    click.echo(f"  Migrated: {migrated_count} entries")
    click.echo(f"  Skipped:  {skipped_count} entries")
    click.echo(f"  Errors:   {error_count} entries")
    if not commit:
        click.echo()
        click.echo("  (Dry run — no data was written. Use --commit to write.)")


if __name__ == "__main__":
    main()
