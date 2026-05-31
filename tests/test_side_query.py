# tests/test_side_query.py
"""Unit tests for scripts/side_query.py (atelier#64 AI-3; design §9.4).

Human→worker side-query recording:

* AC3 — the team_audit_log row shape is correct (event_type='side_query',
  payload {prompt, response, worker_role_id}); it does NOT redirect the worker
  (no task/role mutation) and does NOT replace PM escalation (independent path).
* §9.4 mirror — the durable-backend mirror is BEST-EFFORT: a mirror failure
  must NOT fail the side-query nor drop the canonical audit row; when it
  succeeds, the same prompt+response+role_id appear in both.

Local mode (default — no Memex). The fixture chdir's into a fake-git workspace
with a fully-migrated .ai/atelier.db and a seeded team (team_audit_log FKs to
teams).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backend, side_query
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def team_workspace(tmp_path, monkeypatch):
    """Fake-git workspace with a migrated local DB + a seeded team.

    team_audit_log FKs to teams(team_id), so we seed one team row. Local mode
    is the default (no Memex on the box / in CI)."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
        ("T1", "P1", "team-lead", "active"),
    )
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db)}


def _audit_rows(db: str, event_type: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM team_audit_log WHERE event_type = ? ORDER BY id",
            (event_type,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _task_count(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


# ── AC3: canonical audit row shape ──────────────────────────────────────────


def test_side_query_writes_canonical_audit_row(team_workspace) -> None:
    """The canonical team_audit_log row has event_type='side_query' and a
    payload of {prompt, response, worker_role_id}."""
    out = side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="how is the auth refactor going?",
        response="halfway; blocked on the token rotation contract",
        mirror=False,
    )
    rows = _audit_rows(team_workspace["db"], "side_query")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload == {
        "prompt": "how is the auth refactor going?",
        "response": "halfway; blocked on the token rotation contract",
        "worker_role_id": "backend-engineer-1",
    }
    assert out["audit"]["event_type"] == "side_query"


def test_side_query_does_not_redirect_worker(team_workspace) -> None:
    """A side-query records only — it never mutates the worker's task/role.
    No task rows are created/changed by recording a side-query (§9.4)."""
    before = _task_count(team_workspace["db"])
    side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="status?",
        response="ok",
        mirror=False,
    )
    assert _task_count(team_workspace["db"]) == before == 0


def test_side_query_independent_of_pm_escalation(team_workspace) -> None:
    """Recording a side-query writes ONLY a side_query event — it neither
    raises nor emits any escalation/postmortem event (independent paths)."""
    side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="q",
        response="a",
        mirror=False,
    )
    assert len(_audit_rows(team_workspace["db"], "persona_gap_escalation")) == 0
    assert len(_audit_rows(team_workspace["db"], "meeting_failure_postmortem")) == 0


# ── §9.4 mirror — best-effort ───────────────────────────────────────────────


def test_side_query_mirror_success_same_prompt_response_role(team_workspace, monkeypatch) -> None:
    """When the mirror succeeds, the same prompt+response+role_id flow into the
    durable-backend write (domain=log, subdomain=side-query) — AC3."""
    captured: dict = {}

    def fake_write_document(**kw):
        captured.update(kw)
        return {"id": 99}

    monkeypatch.setattr(backend, "write_document", fake_write_document)

    out = side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="P",
        response="R",
        mirror=True,
    )
    assert out["mirrored"] is True
    assert captured["domain"] == "log"
    assert captured["subdomain"] == "side-query"
    # Same prompt+response+role_id in the mirror metadata.
    assert captured["metadata"]["prompt"] == "P"
    assert captured["metadata"]["response"] == "R"
    assert captured["metadata"]["worker_role_id"] == "backend-engineer-1"
    # And the canonical row carries the identical triple.
    canonical = json.loads(_audit_rows(team_workspace["db"], "side_query")[0]["payload"])
    assert canonical == {"prompt": "P", "response": "R", "worker_role_id": "backend-engineer-1"}


def test_side_query_mirror_failure_does_not_drop_audit_or_raise(
    team_workspace, monkeypatch
) -> None:
    """§9.4: a mirror failure must NOT fail the side-query and MUST NOT drop the
    canonical audit row. The exception is swallowed, the canonical row stands,
    and the failure reason is surfaced for observability."""

    def boom(**kw):
        raise RuntimeError("durable backend unavailable")

    monkeypatch.setattr(backend, "write_document", boom)

    out = side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="P",
        response="R",
        mirror=True,
    )
    # The side-query itself succeeded (no raise) ...
    assert out["mirrored"] is False
    assert "durable backend unavailable" in out["mirror_error"]
    # ... and the canonical audit row is intact.
    rows = _audit_rows(team_workspace["db"], "side_query")
    assert len(rows) == 1


# ── atelier#66 [S3] T6-sq — side-query mirror in Memex mode (#64) ────────────
#
# The existing mirror tests monkeypatch `backend.write_document` (the FACADE
# itself), so the Memex routing branch is never exercised and a Memex-mode
# breakage of the best-effort mirror would be SILENT (the swallow at
# side_query.py:88-92; R2). These two force-Memex tests use the canonical
# hermetic stub set and spy the `backend_memex` LEAF instead — proving (1) the
# mirror reaches the Memex backend with the full triple in metadata while the
# always-Local canonical audit row still lands, and (2) a Memex-mode mirror
# FAILURE is swallowed (mirrored=False / mirror_error set) WITHOUT dropping the
# always-Local audit row. The both-mode SPLIT is the deliverable: the canonical
# audit is §17 always-Local (backend.write_team_audit binds backend_local
# directly, mode-agnostic), the mirror routes through the mode facade.


def test_side_query_mirror_persists_in_memex_mode(team_workspace, monkeypatch) -> None:
    """Force-Memex: the best-effort mirror reaches `backend_memex.write_document`
    (domain=log, subdomain=side-query) with team_id+worker_role_id+prompt+
    response in metadata, while the canonical `team_audit_log` row stays
    always-Local (§17). Spies the Memex leaf — NOT the facade — so the Memex
    routing branch is genuinely exercised."""
    from scripts import backend, backend_memex, mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)

    captured: dict = {}

    def fake_memex_write_document(**kw):
        captured.update(kw)
        return {"row_id": 99, "index_id": "i-99"}

    monkeypatch.setattr(backend_memex, "write_document", fake_memex_write_document)

    out = side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="P",
        response="R",
        mirror=True,
    )

    # The mirror reached the Memex backend leaf (routing branch exercised).
    assert out["mirrored"] is True
    assert out["mirror_error"] is None
    assert captured, "mirror never reached backend_memex.write_document"
    assert captured["domain"] == "log"
    # subdomain may ride native or fold into metadata depending on the facade
    # adapter — accept either, but it MUST be present somewhere.
    assert (
        captured.get("subdomain") == "side-query"
        or captured["metadata"].get("subdomain") == "side-query"
    )
    # The full triple survives into the Memex metadata blob (AC3).
    assert captured["metadata"]["team_id"] == "T1"
    assert captured["metadata"]["worker_role_id"] == "backend-engineer-1"
    assert captured["metadata"]["prompt"] == "P"
    assert captured["metadata"]["response"] == "R"
    # The canonical audit row is always-Local (§17) — it landed in the local DB
    # even though detect_mode is 'memex'.
    canonical = _audit_rows(team_workspace["db"], "side_query")
    assert len(canonical) == 1
    assert json.loads(canonical[0]["payload"]) == {
        "prompt": "P",
        "response": "R",
        "worker_role_id": "backend-engineer-1",
    }


def test_side_query_memex_mirror_failure_is_swallowed(team_workspace, monkeypatch) -> None:
    """Force-Memex: a Memex-backend mirror FAILURE is swallowed
    (mirrored=False / mirror_error set) and does NOT drop the always-Local
    canonical audit row — the §9.4 best-effort guarantee holds on the Memex
    branch too (R2: the swallow is otherwise silent in Memex mode)."""
    from scripts import backend, backend_memex, mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)

    def boom(**kw):
        raise RuntimeError("memex core unavailable")

    monkeypatch.setattr(backend_memex, "write_document", boom)

    out = side_query.record_side_query(
        team_id="T1",
        worker_role_id="backend-engineer-1",
        prompt="P",
        response="R",
        mirror=True,
    )

    assert out["mirrored"] is False
    assert "memex core unavailable" in out["mirror_error"]
    # The canonical always-Local audit row is intact despite the mirror failure.
    assert len(_audit_rows(team_workspace["db"], "side_query")) == 1
