"""End-to-end tests for Local→Memex migration replay (Plan 4 T1 / T27).

A project with a populated local `atelier.db` migrates cleanly into a
fake Memex install: every row reaches the Memex backend, a `.migrated`
marker lands in `.ai/`, and the original DB is renamed to a stable
archive name. Re-running on an already-migrated workspace is a no-op.
A mid-write failure leaves the workspace untouched so the next attempt
can retry.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.migrate import apply_migrations


MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def populated_local_project(tmp_path, monkeypatch):
    """Create a project with .ai/atelier.db containing real rows.

    The fixture chdir's into a tmp workspace, applies both migration
    suites, and seeds rows via the Local-mode backend. Mode detection
    is pinned to "local" so the seeding writes land in the project-local
    DB (rather than recursing into the Memex backend).
    """
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()  # backend_local needs a git root
    monkeypatch.chdir(root)

    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")

    # Pin to Local mode so the seed writes go to .ai/atelier.db rather
    # than recursing into the (uninitialized) Memex backend.
    from scripts import mode_detector
    mode_detector._clear_cache()
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")

    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    from scripts.tasks import create_task
    from scripts.meetings import create_meeting

    role = create_role(str(db), name="Product Manager", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM",
                 role_id=role["id"], profile="pm")
    create_project(str(db), name="myproj",
                   description="auth", created_by="atelier-pm-1")
    create_task(str(db), project_id=1, title="Fix bug",
                description="500 error", created_by="atelier-pm-1")
    create_meeting(str(db), root / ".ai" / "meetings",
                   title="Kickoff", date="2026-05-16",
                   summary="scope", decisions="oauth2",
                   created_by="atelier-pm-1",
                   project_id=1, workspace_id=1)
    return root


def _stub_memex_writes(monkeypatch, captured=None):
    """Common monkeypatching: capture replayed rows + neutralize the
    bootstrap precondition + force `lookup_index_id_by_source_ref` to
    return None so every row is treated as new."""
    if captured is None:
        captured = {"docs": [], "tasks": [], "meetings": [], "sessions": [],
                    "bypasses": []}

    def fake_write_document(**kwargs):
        captured["docs"].append(kwargs)
        return {"status": "ingested", "index_id": f"doc-{len(captured['docs'])}",
                "row_id": len(captured["docs"]),
                "key": kwargs["title"], "domain": kwargs["domain"],
                "relations": []}

    def fake_write_task(**kwargs):
        captured["tasks"].append(kwargs)
        return {"status": "ingested", "index_id": f"tsk-{len(captured['tasks'])}",
                "row_id": len(captured["tasks"]),
                "key": kwargs["title"], "domain": "task", "relations": []}

    def fake_write_meeting(**kwargs):
        captured["meetings"].append(kwargs)
        return {"status": "ingested",
                "index_id": f"mtg-{len(captured['meetings'])}",
                "row_id": len(captured["meetings"]),
                "key": kwargs["title"], "domain": "meeting", "relations": []}

    def fake_upsert_session(**kwargs):
        captured["sessions"].append(kwargs)
        return {"id": len(captured["sessions"]), **kwargs}

    def fake_record_phase_bypass(**kwargs):
        captured["bypasses"].append(kwargs)
        return {"id": len(captured["bypasses"]), **kwargs}

    monkeypatch.setattr("scripts.backend_memex.write_document",
                        fake_write_document)
    monkeypatch.setattr("scripts.backend_memex.write_task",
                        fake_write_task)
    monkeypatch.setattr("scripts.backend_memex.write_meeting",
                        fake_write_meeting)
    monkeypatch.setattr("scripts.backend_memex.upsert_session",
                        fake_upsert_session)
    monkeypatch.setattr("scripts.backend_memex.record_phase_bypass",
                        fake_record_phase_bypass)
    monkeypatch.setattr("scripts.backend_memex.lookup_index_id_by_source_ref",
                        lambda *, source_ref: None)
    monkeypatch.setattr("scripts.backend_memex.require_memex_bootstrap",
                        lambda: None)
    return captured


def test_migration_replays_all_rows(populated_local_project, monkeypatch):
    """After migration, every local row appears in the Memex backend
    and the marker file is written."""
    captured = _stub_memex_writes(monkeypatch)

    from scripts.migrate_to_memex import migrate_project
    summary = migrate_project(populated_local_project / ".ai" / "atelier.db")

    assert summary["status"] == "migrated"
    assert summary["migrated"]["projects"] == 1
    assert summary["migrated"]["tasks"] == 1
    assert summary["migrated"]["meetings"] == 1
    assert (populated_local_project / ".ai" / "atelier.migrated").exists()
    # Captures should reflect the replayed rows.
    assert len(captured["docs"]) == 1  # project replayed as a document
    assert len(captured["tasks"]) == 1
    assert len(captured["meetings"]) == 1


def test_migration_renames_pre_migration_db(populated_local_project,
                                            monkeypatch):
    """A successful migration archives the original DB under a stable
    `atelier-pre-migration-<timestamp>.db` filename."""
    _stub_memex_writes(monkeypatch)

    from scripts.migrate_to_memex import migrate_project
    migrate_project(populated_local_project / ".ai" / "atelier.db")

    assert not (populated_local_project / ".ai" / "atelier.db").exists()
    pre_migration_files = list((populated_local_project / ".ai").glob(
        "atelier-pre-migration-*.db"))
    assert len(pre_migration_files) == 1


def test_migration_failure_leaves_no_marker(populated_local_project,
                                            monkeypatch):
    """If any write fails, no .migrated marker is written and the local
    DB is NOT renamed — so the next Atelier command retries cleanly."""
    _stub_memex_writes(monkeypatch)

    def boom(**k):
        raise RuntimeError("simulated memex outage")
    monkeypatch.setattr("scripts.backend_memex.write_document", boom)

    from scripts.migrate_to_memex import migrate_project
    with pytest.raises(RuntimeError):
        migrate_project(populated_local_project / ".ai" / "atelier.db")
    assert (populated_local_project / ".ai" / "atelier.db").exists()
    assert not (populated_local_project / ".ai" / "atelier.migrated").exists()


def test_migration_skipped_when_marker_exists(populated_local_project,
                                              monkeypatch):
    """If the marker is already there, migrate_project returns 'skipped'
    immediately — no Memex contact, no rename."""
    _stub_memex_writes(monkeypatch)
    marker = populated_local_project / ".ai" / "atelier.migrated"
    marker.write_text('{"migrated_at": "2026-01-01"}')

    from scripts.migrate_to_memex import migrate_project
    summary = migrate_project(populated_local_project / ".ai" / "atelier.db")
    assert summary["status"] == "skipped"


def test_decline_writes_local_only_marker(populated_local_project):
    """User declines migration → .local-only marker is written and
    subsequent commands won't re-prompt."""
    from scripts.migrate_to_memex import decline_migration
    decline_migration(populated_local_project / ".ai")
    assert (populated_local_project / ".ai" / "atelier.local-only").exists()
