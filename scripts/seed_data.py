# scripts/seed_data.py
"""Load Atelier's shipped role + agent seed data.

Both Memex bootstrap (memex:core:register-role / register-agent) and
Local-mode INSERT paths read from the same JSON files in templates/.
"""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def load_role_seed() -> list[dict]:
    """Return the canonical role catalog as a list of {name, description}."""
    data = json.loads((_TEMPLATES / "roles.json").read_text(encoding="utf-8"))
    return data["roles"]


# Agent loader added by Task 4.
