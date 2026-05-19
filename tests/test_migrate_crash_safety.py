"""Verify migration is non-destructive on partial failure and that
re-running after a fix completes cleanly."""

from pathlib import Path
import pytest


@pytest.fixture
def project_with_data(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    from scripts.migrate import apply_migrations

    MIGRATIONS = Path(__file__).parent.parent / "migrations"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    from scripts.tasks import create_task

    r = create_role(str(db), name="PM", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM", role_id=r["id"], profile="x")
    for i in range(5):
        create_project(str(db), name=f"P{i}", description="d", created_by="atelier-pm-1")
    for i in range(10):
        create_task(
            str(db), project_id=1, title=f"T{i}", description="d", created_by="atelier-pm-1"
        )
    return root


def test_failure_during_task_replay_leaves_no_marker(project_with_data, monkeypatch):
    """Inject failure on the 3rd task write. No marker is written, the
    local DB is not renamed, and a re-run succeeds when the issue clears."""
    fail_after = {"count": 0, "limit": 3}

    def flaky_write_task(**kwargs):
        fail_after["count"] += 1
        if fail_after["count"] > fail_after["limit"]:
            raise RuntimeError("simulated memex outage")
        return {
            "row_id": fail_after["count"],
            "index_id": "x",
            "key": "k",
            "domain": "task",
            "relations": [],
        }

    monkeypatch.setattr(
        "scripts.backend_memex.write_document",
        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "d", "relations": []},
    )
    monkeypatch.setattr("scripts.backend_memex.write_task", flaky_write_task)
    monkeypatch.setattr(
        "scripts.backend_memex.lookup_index_id_by_source_ref", lambda *, source_ref: None
    )
    monkeypatch.setattr("scripts.backend_memex.require_memex_bootstrap", lambda: None)
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})

    from scripts.migrate_to_memex import migrate_project

    with pytest.raises(RuntimeError):
        migrate_project(project_with_data / ".ai" / "atelier.db")

    # Local DB intact
    assert (project_with_data / ".ai" / "atelier.db").exists()
    assert not (project_with_data / ".ai" / "atelier.migrated").exists()


def test_rerun_after_outage_is_idempotent(project_with_data, monkeypatch):
    """After the imaginary outage clears, re-running the migration
    succeeds. Idempotency is the responsibility of atelier's replay
    layer: before writing a row, `migrate_project` looks up the
    atelier source_ref (e.g., `atelier:tasks:42`) in the Memex Index
    via `_index_id_for_atelier_row()`. If found, the row is skipped
    and counted under `summary['already_present']`.

    Memex's `librarian.write_entry` raises `DuplicateKeyError` on key
    collision (v2.3.0+), so atelier must NOT rely on memex silently
    deduping — the precheck happens client-side.
    """
    # Simulate Index lookups that report "first 2 tasks already present
    # from the prior partial run". Remaining writes succeed normally.
    already_seen = {"atelier:tasks:1", "atelier:tasks:2"}

    def fake_index_lookup(source_ref: str) -> str | None:
        return "01t-prev" if source_ref in already_seen else None

    monkeypatch.setattr(
        "scripts.migrate_to_memex._index_id_for_atelier_row",
        fake_index_lookup,
    )
    monkeypatch.setattr(
        "scripts.backend_memex.write_document",
        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "d", "relations": []},
    )
    monkeypatch.setattr(
        "scripts.backend_memex.write_task",
        lambda **k: {"row_id": 1, "index_id": "x", "key": "k", "domain": "task", "relations": []},
    )
    monkeypatch.setattr(
        "scripts.backend_memex.write_meeting",
        lambda **k: {
            "row_id": 1,
            "index_id": "x",
            "key": "k",
            "domain": "meeting",
            "relations": [],
        },
    )
    monkeypatch.setattr("scripts.backend_memex.require_memex_bootstrap", lambda: None)
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})

    from scripts.migrate_to_memex import migrate_project

    summary = migrate_project(project_with_data / ".ai" / "atelier.db")
    assert summary["status"] == "migrated"
    # Two tasks should have been skipped as already-present
    assert summary.get("already_present", {}).get("tasks", 0) == 2
    assert (project_with_data / ".ai" / "atelier.migrated").exists()
