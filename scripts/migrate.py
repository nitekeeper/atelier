import sqlite3
from pathlib import Path
from datetime import datetime, timezone

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + FK enforcement.

    Inlined into the migration runner because `scripts/db.py` was retired
    (Plan 3 Task 9). The migration runner is the only legitimate consumer
    of a raw SQLite handle that survives the Memex/Local backend split;
    business-logic callers go through `scripts/backend.py`.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    conn = get_connection(db_path)
    # Naming convention: shared/ uses 001-049, local-only/ uses 050+.
    # The `migrations.filename` column is UNIQUE per file (not per directory) --
    # a collision between shared/050_foo.sql and local-only/050_foo.sql would
    # silently skip the second one. Enforce by convention.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            filename   TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

    for migration_file in sorted(migrations_dir.glob("*.sql")):
        if migration_file.name in applied:
            continue
        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
            (migration_file.name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    conn.close()


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else ".ai/memex.db"
    # Local-mode default: apply shared/ then local-only/.
    # Memex-mode bootstrap supplies only shared/ via memex:core:create-store.
    shared = MIGRATIONS_DIR / "shared"
    local = MIGRATIONS_DIR / "local-only"
    apply_migrations(db_path, shared)
    apply_migrations(db_path, local)
    print(f"Migrations applied to {db_path}")
