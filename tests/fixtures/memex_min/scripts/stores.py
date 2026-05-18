"""Store provisioning — minimal stub.

Trimmed copy of memex/scripts/stores.py. Only `create_store` is exercised
by atelier bootstrap; the other CRUD helpers are omitted.
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


def create_store(name: str, path: str, migrations_dir: str,
                 schema_version: str = "v1") -> dict:
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
