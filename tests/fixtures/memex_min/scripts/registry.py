"""Store registry — minimal stub.

Trimmed copy of memex/scripts/registry.py. Same JSON-on-disk format
(flat `{name: record}` map) so atelier's bootstrap sees the registry
shape it expects.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.db import memex_home


def _registry_path() -> Path:
    return memex_home() / "registry.json"


def _load() -> dict:
    p = _registry_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _save(data: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def register_store(name: str, path: str, schema_version: str) -> dict:
    data = _load()
    if name in data:
        raise ValueError(f"Store already registered: {name}")
    record = {
        "name": name,
        "path": path,
        "schema_version": schema_version,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    data[name] = record
    _save(data)
    return record


def get_store(name: str) -> dict | None:
    return _load().get(name)
