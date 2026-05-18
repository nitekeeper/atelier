# tests/test_seed_roles.py
import json
import pytest
from pathlib import Path
from scripts.migrate import apply_migrations
from scripts.seed_data import load_role_seed
from scripts.seed_roles import seed, ROLES

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Hermetic Local-mode workspace fixture.

    `scripts.seed_roles.seed` routes through `scripts.backend`, which
    dispatches per `mode_detector.detect_mode()`. On a dev host where
    `~/.memex/` is populated the facade would resolve to Memex mode and
    write to `~/.memex/agents.db` — the test DB at `tmp_path/test.db`
    would never receive any rows, novelty counts would collapse to
    `(0, 0)`, and the per-row exists-assertions below would all see None.

    Round-2 fix: build a fake git workspace under `tmp_path`, chdir into
    it (so `backend_local._workspace_root()` resolves there), and force
    `detect_mode()` to "local" regardless of dev-host state. The autouse
    `_clear_mode_cache` fixture in `tests/conftest.py` keeps the patched
    callable from leaking between tests.
    """
    workspace = tmp_path / "ws"
    (workspace / ".git").mkdir(parents=True)  # marker for find_git_root
    (workspace / ".ai").mkdir()
    path = workspace / ".ai" / "atelier.db"
    apply_migrations(str(path), MIGRATIONS_DIR / "shared")
    apply_migrations(str(path), MIGRATIONS_DIR / "local-only")
    monkeypatch.chdir(workspace)  # backend_local._workspace_root resolves here
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    return str(path)


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
    # Non-vacuous assertion: a `(0, 0)` return from a backend that never
    # wrote to this DB would also satisfy the lines above. Pin the actual
    # row count to len(ROLES) so the test fails if seed silently
    # short-circuits without inserting anything.
    from scripts.migrate import get_connection
    conn = get_connection(db_path)
    try:
        role_count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    finally:
        conn.close()
    assert role_count == len(ROLES), f"expected {len(ROLES)} roles, got {role_count}"
    assert agent_count == len(ROLES), f"expected {len(ROLES)} agents, got {agent_count}"


def test_seed_no_duplicate_role_names(db_path):
    seed(db_path)
    from scripts.migrate import get_connection
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT name, COUNT(*) as cnt FROM roles GROUP BY name HAVING cnt > 1"
    ).fetchall()
    # Non-vacuous assertions: "no duplicates among zero rows" is a
    # tautology. Pin total_rows > 0 AND distinct_names == len(ROLES)
    # so the test fails if seed routed away from this DB.
    total_rows = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    distinct_names = conn.execute("SELECT DISTINCT name FROM roles").fetchall()
    conn.close()
    assert rows == [], f"Duplicate role names found: {rows}"
    assert total_rows > 0, "seed did not insert into the test DB"
    assert len(distinct_names) == len(ROLES), (
        f"expected {len(ROLES)} distinct role names, got {len(distinct_names)}"
    )


def test_seed_pm_role_exists(db_path):
    seed(db_path)
    from scripts.migrate import get_connection
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM roles WHERE name = 'Product Manager'").fetchone()
    conn.close()
    assert row is not None


def test_seed_systems_engineer_exists(db_path):
    seed(db_path)
    from scripts.migrate import get_connection
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


# ---------------------------------------------------------------------------
# Plan 1 Task 3: Atelier role seed JSON + loader (templates/roles.json).
# These tests pin the JSON-as-source-of-truth contract that Memex bootstrap
# and Local-mode INSERT paths both consume.
# ---------------------------------------------------------------------------


def test_role_seed_file_exists():
    assert (TEMPLATES_DIR / "roles.json").exists()


def test_role_seed_returns_list_of_dicts():
    """Shape: list of {name, description} dicts. Length floor lives in its own
    test (test_role_seed_has_at_least_46_roles) so failure messages stay precise."""
    roles = load_role_seed()
    assert isinstance(roles, list)
    for r in roles:
        assert {"name", "description"} <= r.keys()


def test_role_seed_has_at_least_46_roles():
    """The shipped catalog has ~61 personas (see scripts/seed_roles.py ROLES).
    We assert a floor of 46 to keep the test resilient to additions/removals."""
    roles = load_role_seed()
    assert len(roles) >= 46, f"expected at least 46 roles, got {len(roles)}"


def test_role_seed_has_canonical_atelier_roles():
    roles = load_role_seed()
    names = {r["name"] for r in roles}
    # Canonical PM name is "Product Manager" — see scripts/seed_roles.py:22
    # and the existing test_seed_pm_role_exists in this file.
    assert "Product Manager" in names
    assert "Software Architect" in names


def test_role_seed_pm_canonical_name():
    """Product Manager exists with canonical name in JSON seed (mirrors
    test_seed_pm_role_exists which only verifies the DB-seeded form)."""
    names = {r["name"] for r in load_role_seed()}
    assert "Product Manager" in names


def test_role_seed_names_are_unique():
    roles = load_role_seed()
    names = [r["name"] for r in roles]
    assert len(names) == len(set(names))


def test_role_seed_matches_seed_roles_module():
    """The JSON file is the source of truth; this test pins parity with the
    existing seed_roles.ROLES list so Plan 4's migrator can swap one for
    the other without behavior change. Pins both name and description bytes
    so a silent rewrite of either field would fail the test."""
    from scripts.seed_roles import ROLES as LEGACY_ROLES
    json_names = {r["name"] for r in load_role_seed()}
    legacy_names = {r["role_name"] for r in LEGACY_ROLES}
    assert json_names == legacy_names
    json_pairs = {(r["name"], r["description"]) for r in load_role_seed()}
    legacy_pairs = {(r["role_name"], r["role_desc"]) for r in LEGACY_ROLES}
    assert json_pairs == legacy_pairs


def test_role_seed_load_is_deterministic():
    """Loading the seed twice yields equal dict contents — guards against
    accidental nondeterministic ordering in a future regenerator."""
    first = load_role_seed()
    second = load_role_seed()
    assert first == second


def test_role_seed_file_bytes_are_stable():
    """Reading templates/roles.json twice yields identical bytes — guards
    the regenerator from emitting different bytes for the same content."""
    path = TEMPLATES / "roles.json"
    first = path.read_bytes()
    second = path.read_bytes()
    assert first == second


def test_role_seed_json_envelope():
    """Top-level JSON shape is {"roles": [...]} — flattening to a bare array
    would break the loader."""
    raw = (TEMPLATES / "roles.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert set(data.keys()) == {"roles"}
    assert isinstance(data["roles"], list)
