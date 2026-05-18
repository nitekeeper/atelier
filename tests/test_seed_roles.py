# tests/test_seed_roles.py
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.seed_roles import seed, ROLES

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    apply_migrations(path, MIGRATIONS_DIR / "shared")
    apply_migrations(path, MIGRATIONS_DIR / "local-only")
    return path


def test_seed_inserts_all_roles(db_path):
    roles_added, _ = seed(db_path)
    assert roles_added == len(ROLES)


def test_seed_inserts_one_agent_per_role(db_path):
    _, agents_added = seed(db_path)
    assert agents_added == len(ROLES)


def test_seed_is_idempotent(db_path):
    seed(db_path)
    roles_added, agents_added = seed(db_path)
    assert roles_added == 0
    assert agents_added == 0


def test_seed_no_duplicate_role_names(db_path):
    seed(db_path)
    from scripts.db import get_connection
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT name, COUNT(*) as cnt FROM roles GROUP BY name HAVING cnt > 1"
    ).fetchall()
    conn.close()
    assert rows == [], f"Duplicate role names found: {rows}"


def test_seed_pm_role_exists(db_path):
    seed(db_path)
    from scripts.db import get_connection
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM roles WHERE name = 'Product Manager'").fetchone()
    conn.close()
    assert row is not None


def test_seed_systems_engineer_exists(db_path):
    seed(db_path)
    from scripts.db import get_connection
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM agents WHERE id = 'systems-engineer-1'").fetchone()
    conn.close()
    assert row is not None


def test_all_profiles_have_required_sections(db_path):
    seed(db_path)
    for entry in ROLES:
        profile = entry["agent_profile"]
        assert "Expertise:" in profile, f"Missing Expertise in {entry['role_name']}"
        assert "Responsibilities:" in profile, f"Missing Responsibilities in {entry['role_name']}"
        assert "Works with:" in profile, f"Missing Works with in {entry['role_name']}"
        assert "Does not:" in profile, f"Missing Does not in {entry['role_name']}"
        assert "Communication style:" in profile, f"Missing Communication style in {entry['role_name']}"
