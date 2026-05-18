"""Wave-0 tests for Atelier shipped agent profile seed + loader.

These tests pin the contract for `templates/agents/*.json` and the
`load_agent_seed()` loader in `scripts/seed_data.py`. The JSON files
mirror `scripts/seed_roles.py:ROLES` byte-for-byte at the persona
profile level — Plan 4's migrator and both bootstrap paths (Memex
`register-agent` and Local INSERT) depend on this parity.
"""
from pathlib import Path

from scripts.seed_data import load_agent_seed

TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "agents"


def test_agents_directory_exists():
    assert TEMPLATES_DIR.is_dir()


def test_agents_directory_has_expected_count():
    """One JSON file per persona in scripts/seed_roles.py:ROLES."""
    files = list(TEMPLATES_DIR.glob("*.json"))
    assert len(files) >= 46, f"expected at least 46 agent files, got {len(files)}"


def test_load_agent_seed_returns_at_least_46_agents():
    agents = load_agent_seed()
    assert len(agents) >= 46


def test_each_agent_has_required_fields():
    for a in load_agent_seed():
        assert {"agent_id", "name", "role_name", "profile"} <= a.keys()
        assert isinstance(a["profile"], str) and len(a["profile"]) > 100


def test_agent_ids_unique():
    agents = load_agent_seed()
    ids = [a["agent_id"] for a in agents]
    assert len(ids) == len(set(ids))


def test_agent_role_names_match_role_seed():
    """Every agent.role_name must resolve to a known role in roles.json.

    The loader does not perform the resolution (that's done at bootstrap
    after memex:core:register-role returns the new role_id int PK); this
    test only enforces referential integrity at the name level.
    """
    from scripts.seed_data import load_role_seed
    role_names = {r["name"] for r in load_role_seed()}
    for a in load_agent_seed():
        assert a["role_name"] in role_names, \
            f"agent {a['agent_id']} references unknown role {a['role_name']}"


def test_agent_seed_matches_seed_roles_module():
    """Parity with the legacy seed_roles.ROLES list — Plan 4's migrator
    needs to know the JSON files are an exact mirror.
    """
    from scripts.seed_roles import ROLES as LEGACY_ROLES
    json_ids = {a["agent_id"] for a in load_agent_seed()}
    legacy_ids = {r["agent_id"] for r in LEGACY_ROLES}
    assert json_ids == legacy_ids
