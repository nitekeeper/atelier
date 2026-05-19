"""Plugin-anchored filesystem constants — minimal stub.

Trimmed copy of memex/scripts/paths.py with the import-time bundle-layout
assertion preserved so a malformed fixture surfaces at import time.
"""

from __future__ import annotations

from pathlib import Path

# scripts/paths.py -> scripts/ -> <plugin_root>
PLUGIN_ROOT: Path = Path(__file__).resolve().parent.parent
DB_DIR: Path = PLUGIN_ROOT / "db"

if not (DB_DIR / "migrations_table.sql").is_file():
    raise ImportError(
        f"Memex fixture bundle layout broken: {DB_DIR}/migrations_table.sql not found."
    )
