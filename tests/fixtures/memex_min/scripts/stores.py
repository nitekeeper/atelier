"""Store provisioning — minimal stub.

Trimmed copy of memex/scripts/stores.py. `create_store` (first-provision) and
`migrate` (idempotent shared-migration reconciliation on an EXISTING store) are
exercised by atelier bootstrap; the other CRUD helpers are omitted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts import registry
from scripts.db import get_connection, require_bootstrap
from scripts.paths import DB_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrations_table_sql() -> str:
    return (DB_DIR / "migrations_table.sql").read_text()


def create_store(name: str, path: str, migrations_dir: str, schema_version: str = "v1") -> dict:
    require_bootstrap()
    if registry.get_store(name) is not None:
        raise ValueError(f"Store already registered: {name}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    conn.executescript(_migrations_table_sql())
    conn.commit()
    sql_files = sorted(Path(migrations_dir).glob("*.sql"))
    for sql_file in sql_files:
        conn.executescript(sql_file.read_text())
        conn.execute(
            "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
            (sql_file.name, _now()),
        )
    conn.commit()
    conn.close()
    return registry.register_store(name, path, schema_version)


def migrate(name: str, migrations_dir: str) -> list[str]:
    """Apply unapplied .sql files from migrations_dir to a registered store.

    Trimmed copy of memex/scripts/stores.py::migrate. Idempotent — skips files
    already recorded in the store's `migrations` table. Returns the list of
    newly-applied filenames. Raises ValueError if the store is unregistered.
    """
    require_bootstrap()
    rec = registry.get_store(name)
    if rec is None:
        raise ValueError(f"Unknown store: {name}")

    conn = get_connection(rec["path"])
    applied_set = {r["filename"] for r in conn.execute("SELECT filename FROM migrations")}

    sql_files = sorted(Path(migrations_dir).glob("*.sql"))
    newly_applied: list[str] = []
    for sql_file in sql_files:
        if sql_file.name in applied_set:
            continue
        conn.executescript(sql_file.read_text())
        conn.execute(
            "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
            (sql_file.name, _now()),
        )
        newly_applied.append(sql_file.name)
    conn.commit()
    conn.close()
    return newly_applied
