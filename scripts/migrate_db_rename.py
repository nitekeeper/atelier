"""Detect coexisting atelier.db and memex.db and report migration status.

Usage:
    python3 scripts/migrate_db_rename.py [<directory>]

Default directory: .ai/

Exit codes:
    0 — no ambiguity (at most one DB file found)
    1 — both atelier.db and memex.db detected; manual resolution required
"""

from __future__ import annotations

import sys
from pathlib import Path


def _db_info(path: Path) -> dict:
    size = path.stat().st_size
    try:
        import sqlite3

        conn = sqlite3.connect(str(path))
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        conn.close()
    except Exception as exc:
        table_count = f"error: {exc}"
    return {"path": str(path), "size_bytes": size, "tables": table_count}


def check(directory: Path) -> int:
    atelier = directory / "atelier.db"
    memex = directory / "memex.db"

    has_atelier = atelier.exists()
    has_memex = memex.exists()

    if not has_atelier and not has_memex:
        print(f"No DB files found in {directory}. Nothing to do.")
        return 0

    if has_atelier and not has_memex:
        print("Found only atelier.db — migration to memex.db not yet done.")
        print(f"To migrate: copy {atelier} to {memex}, then delete {atelier}.")
        return 0

    if has_memex and not has_atelier:
        info = _db_info(memex)
        print(
            f"OK: memex.db present ({info['size_bytes']} bytes, {info['tables']} tables). No atelier.db. Nothing to do."
        )
        return 0

    # Both exist
    ai = _db_info(atelier)
    mi = _db_info(memex)
    print("AMBIGUITY DETECTED: both atelier.db and memex.db exist.")
    print()
    print(f"  atelier.db  — {ai['size_bytes']:>10} bytes, {ai['tables']} tables  ({atelier})")
    print(f"  memex.db    — {mi['size_bytes']:>10} bytes, {mi['tables']} tables  ({memex})")
    print()
    newer = "atelier.db" if atelier.stat().st_mtime > memex.stat().st_mtime else "memex.db"
    larger = "atelier.db" if ai["size_bytes"] > mi["size_bytes"] else "memex.db"
    print(f"  Newer file : {newer}")
    print(f"  Larger file: {larger}")
    print()
    print("Resolution (manual — do NOT automate):")
    print("  1. Verify which file is authoritative (usually the larger/newer one).")
    print("  2. If memex.db is authoritative: delete atelier.db.")
    print("  3. If atelier.db is authoritative: copy it to memex.db, then delete atelier.db.")
    print("  4. Re-run this script to confirm clean state.")
    return 1


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".ai")
    try:
        sys.exit(check(directory))
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
