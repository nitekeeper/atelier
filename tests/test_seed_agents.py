"""Wave-0 tests for Atelier shipped agent profile seed + loader.

These tests pin the contract for `templates/agents/*.json` and the
`load_agent_seed()` loader in `scripts/seed_data.py`. The JSON files
mirror `scripts/seed_roles.py:ROLES` byte-for-byte at the persona
profile level — Plan 4's migrator and both bootstrap paths (Memex
`register-agent` and Local INSERT) depend on this parity.
"""

import json
from pathlib import Path

import pytest

from scripts.seed_data import load_agent_seed, load_role_seed
from scripts.seed_roles import ROLES as LEGACY_ROLES

TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "agents"


def test_agents_directory_exists():
    assert TEMPLATES_DIR.is_dir()


def test_agents_directory_has_expected_count():
    """One JSON file per persona in scripts/seed_roles.py:ROLES."""
    files = list(TEMPLATES_DIR.glob("*.json"))
    assert len(files) == len(LEGACY_ROLES), (
        f"expected {len(LEGACY_ROLES)} agent files, got {len(files)}"
    )


def test_load_agent_seed_matches_legacy_role_count():
    agents = load_agent_seed()
    assert len(agents) == len(LEGACY_ROLES)


def test_load_agent_seed_returns_lexicographic_order():
    agents = load_agent_seed()
    ids = [a["agent_id"] for a in agents]
    assert ids == sorted(ids)


def test_each_agent_has_required_keys():
    required = {"agent_id", "name", "role_name", "profile"}
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data.keys()) == required, (
            f"{path.name}: unexpected keys: {set(data.keys()) - required}"
        )


def test_each_agent_profile_is_nontrivial_string():
    for a in load_agent_seed():
        assert isinstance(a["profile"], str), f"{a['agent_id']}: profile must be str"
        assert len(a["profile"]) >= 500, (
            f"{a['agent_id']}: profile too short ({len(a['profile'])} < 500)"
        )


def test_agent_ids_unique():
    agents = load_agent_seed()
    ids = [a["agent_id"] for a in agents]
    assert len(ids) == len(set(ids))


def test_agent_names_unique():
    names = [a["name"] for a in load_agent_seed()]
    assert len(names) == len(set(names))


def test_agent_filename_matches_agent_id():
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert path.stem == data["agent_id"], (
            f"{path.name} disagrees with its agent_id {data['agent_id']!r}"
        )


def test_agent_role_names_match_role_seed():
    """Every agent.role_name must resolve to a known role in roles.json,
    and every role must be covered by exactly one agent. The loader does
    not perform role_id resolution (that happens at bootstrap after
    memex:core:register-role returns the new role_id int PK); this test
    enforces referential integrity at the name level in both directions.
    """
    agent_role_names = {a["role_name"] for a in load_agent_seed()}
    role_names = {r["name"] for r in load_role_seed()}
    assert agent_role_names == role_names, (
        f"orphan agents: {agent_role_names - role_names}; "
        f"orphan roles: {role_names - agent_role_names}"
    )


def test_agent_seed_matches_seed_roles_module():
    """Parity with the legacy seed_roles.ROLES list — Plan 4's migrator
    needs to know the JSON files are an exact mirror.
    """
    json_ids = {a["agent_id"] for a in load_agent_seed()}
    legacy_ids = {r["agent_id"] for r in LEGACY_ROLES}
    assert json_ids == legacy_ids


def test_load_agent_seed_raises_on_missing_required_key(tmp_path, monkeypatch):
    bad = tmp_path / "agents"
    bad.mkdir()
    (bad / "bad.json").write_text('{"agent_id":"x","name":"y"}', encoding="utf-8")
    monkeypatch.setattr("scripts.seed_data._AGENTS_DIR", bad)
    with pytest.raises(ValueError, match="missing keys"):
        load_agent_seed()


def test_load_agent_seed_raises_on_wrong_type(tmp_path, monkeypatch):
    bad = tmp_path / "agents"
    bad.mkdir()
    (bad / "bad.json").write_text(
        '{"agent_id":"x","name":"y","role_name":"z","profile":42}',
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.seed_data._AGENTS_DIR", bad)
    with pytest.raises(TypeError, match="must be str"):
        load_agent_seed()


def test_load_agent_seed_raises_on_duplicate_agent_id(tmp_path, monkeypatch):
    bad = tmp_path / "agents"
    bad.mkdir()
    for slug in ("a", "b"):
        (bad / f"{slug}.json").write_text(
            '{"agent_id":"dup","name":"x","role_name":"y","profile":"...long enough..."}',
            encoding="utf-8",
        )
    monkeypatch.setattr("scripts.seed_data._AGENTS_DIR", bad)
    with pytest.raises(ValueError, match="duplicate agent_id"):
        load_agent_seed()
