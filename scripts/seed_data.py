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

def load_agent_seed() -> list[dict]:
    """Return Atelier's shipped agent profiles as a list of dicts with
    keys: agent_id, name, role_name, profile.

    Iterates templates/agents/*.json in lexicographic order. Callers must
    resolve role_name → role_id via the roles table AFTER bootstrap has
    registered roles (Memex returns role_id from register-role; Local
    selects MAX(id) after the INSERT).
    """
    agents_dir = _TEMPLATES / "agents"
    profiles: list[dict] = []
    for path in sorted(agents_dir.glob("*.json")):
        profiles.append(json.loads(path.read_text(encoding="utf-8")))
    return profiles
