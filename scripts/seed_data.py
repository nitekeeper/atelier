"""Load Atelier's shipped role + agent seed data.

Both Memex bootstrap (memex:core:register-role / register-agent) and
Local-mode INSERT paths read from the same JSON files in templates/.
"""

from __future__ import annotations

import json
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_AGENTS_DIR = _TEMPLATES / "agents"


def load_role_seed() -> list[dict]:
    """Return the canonical role catalog as a list of {name, description}.

    Validates the shape of each entry: both keys present, both str.
    Raises ValueError on missing keys, TypeError on wrong types.
    """
    data = json.loads((_TEMPLATES / "roles.json").read_text(encoding="utf-8"))
    roles = data["roles"]
    required = {"name", "description"}
    seen_names: set[str] = set()
    for i, role in enumerate(roles):
        if not required <= role.keys():
            missing = required - role.keys()
            raise ValueError(f"roles.json[{i}]: missing keys: {sorted(missing)}")
        for k in required:
            if not isinstance(role[k], str):
                raise TypeError(f"roles.json[{i}]: '{k}' must be str, got {type(role[k]).__name__}")
        if role["name"] in seen_names:
            raise ValueError(f"roles.json[{i}]: duplicate role name '{role['name']}'")
        seen_names.add(role["name"])
    return roles


def load_agent_seed() -> list[dict]:
    """Return Atelier's shipped agent profiles as a list of dicts with
    keys: agent_id, name, role_name, profile.

    Iterates templates/agents/*.json in lexicographic order. Validates the
    shape of each file: all four keys present, all str-typed, agent_id
    unique across the directory. Callers must resolve role_name → role_id
    via the roles table AFTER bootstrap has registered roles (Memex
    returns role_id from register-role; Local selects MAX(id) after the
    INSERT).

    Raises:
        ValueError on missing required keys or duplicate agent_id.
        TypeError when any required field is not a str.
    """
    profiles: list[dict] = []
    seen_ids: set[str] = set()
    required = {"agent_id", "name", "role_name", "profile"}
    for path in sorted(_AGENTS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not required <= data.keys():
            missing = required - data.keys()
            raise ValueError(f"{path}: missing keys: {sorted(missing)}")
        for k in required:
            if not isinstance(data[k], str):
                raise TypeError(f"{path}: '{k}' must be str, got {type(data[k]).__name__}")
        if data["agent_id"] in seen_ids:
            raise ValueError(f"{path}: duplicate agent_id '{data['agent_id']}'")
        seen_ids.add(data["agent_id"])
        profiles.append(data)
    return profiles
