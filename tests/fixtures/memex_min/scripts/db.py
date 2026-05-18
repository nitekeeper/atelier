"""Database connection + Memex home helpers — minimal stub.

Trimmed copy of memex/scripts/db.py. Honors $MEMEX_HOME so the
fake_home fixture's monkeypatch redirects the stub the same way it
redirects the real Memex package.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

MEMEX_DIR_NAME = ".memex"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_identifier(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError(f"identifier must be str, got {type(name).__name__}")
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def get_connection(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        conn.close()
        raise RuntimeError(f"Could not enable WAL mode (got {mode!r})")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class MemexHomeInvalidError(ValueError):
    pass


class MemexNotInitializedError(RuntimeError):
    pass


def memex_home() -> Path:
    explicit = os.environ.get("MEMEX_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path.home() / MEMEX_DIR_NAME


def require_bootstrap() -> None:
    home = memex_home()
    if not (home / "registry.json").exists():
        raise MemexNotInitializedError(
            f"Memex is not bootstrapped at {home}."
        )
