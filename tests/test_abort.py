"""pytest suite for `scripts/abort.py` — team-mode SOFT + HARD abort (atelier#65).

`scripts.abort.main` is the deliberate, in-session abort recorder for the
CURRENT team. It runs in Local mode (mode_detector.detect_mode() == 'local';
migration-006 dispatch-state mutators raise NotImplementedError otherwise), so
these tests stand up a real Local-mode atelier DB (mirroring the `workspace`
fixture pattern in `tests/test_pm_dispatch.py`): chdir into a fake git root,
apply ALL migrations (shared + local-only) against `.ai/atelier.db`, then INSERT
a `teams` row (so the 'aborted' team_audit_log write — which FKs teams — holds).
team_id is supplied explicitly via `--team-id`.

Both paths run one shared core whose AC#2 invariant is:
  * the abort-report doc PERSISTS (domain='postmortem', subdomain='abort'),
  * teams.status is mutated (soft -> 'shutting_down', hard -> 'closed'),
  * a 'aborted' team_audit event is written,
and the worktree is never destroyed when dirty.

AC#2 durability now holds in BOTH modes (atelier#90 part-3): the
abort-report is a workspace-less doc (workspace_id=None) and the Memex
facade `backend.write_document` lands it via the §6.7 `_no-workspace_`
key, so `test_non_local_mode_abort_report_persists` pins cross-mode
persistence. Only the report write crosses modes — teams.status /
'aborted' audit remain Local-mode-only skips in non-local mode
(`test_non_local_mode_skips_state_mutations_and_returns_zero`).

The worktree policy is exercised through the two private seams
(`_worktree_is_dirty` / `_remove_worktree`) monkeypatched per-test, because the
fixture's bare `.git` directory is not a real linked worktree — patching the
seams lets us assert "dirty -> preserved (never removed)" and "clean -> removed"
deterministically without standing up a real git worktree.

The doc-persistence invariant is the AC-CRITICAL parametrized test
`test_abort_report_persists_both_paths`; an explicit non-vacuity guard
(`test_doc_persistence_is_non_vacuous`) FAILS if the `write_document` call is
removed from the implementation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import abort, backend, mode_detector
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

TEAM_ID = "team-abc123"
TEAM_PK = "run-2026-05-31-cycle-1"


# ── Local-mode DB fixture (mirrors tests/test_pm_dispatch.py) ────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace, seeded with
    a `teams` row.

    `backend_local._conn()` and `abort`'s `--db .ai/atelier.db` both resolve via
    the CWD git root, so we chdir into the workspace and drop a `.git` dir.
    `detect_mode` is forced to 'local' so abort.py performs the full teardown
    (state mutation is Local-mode-only)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    now = "2026-05-31T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    # teams row: team_audit_log.team_id REFERENCES teams(team_id), so the
    # 'aborted' audit write requires this row to pre-exist. abort takes team_id
    # explicitly via --team-id, so the FK holds against this seeded row.
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status, schema_version, created_at) "
        "VALUES (?, ?, ?, 'active', 1, ?)",
        (TEAM_ID, "proj-1", "atelier-pm-1", now),
    )
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db)}


# ── Direct-SQL readback helpers ─────────────────────────────────────────────


def _team_status(db_path, team_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _abort_report_docs(db_path):
    """Read the abort-report docs back via the project_documents store —
    domain='postmortem', subdomain='abort'. This is the same store
    `backend.find_documents` queries; we read it directly for an exact-count
    assertion independent of FTS ranking."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM project_documents "
            "WHERE domain = 'postmortem' AND subdomain = 'abort' ORDER BY id",
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── SOFT abort ──────────────────────────────────────────────────────────────


def test_soft_abort_full_teardown_and_dirty_worktree_preserved(workspace, monkeypatch):
    """SOFT abort: teams.status -> 'shutting_down', a 'aborted' team_audit event
    written, the abort-report doc persists (readable back via project_documents),
    AND a DIRTY worktree is PRESERVED (never removed). main() returns 0."""
    # Worktree is DIRTY: the removal seam must never be called.
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    removed_calls = []
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: removed_calls.append(cwd) or True)

    rc = abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID])
    assert rc == 0

    db = workspace["db"]
    # teams.status flipped to the soft target.
    assert _team_status(db, TEAM_ID) == "shutting_down"

    # EXACTLY ONE 'aborted' audit event, scoped to the team, mode='soft'.
    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    assert len(audits) == 1
    assert json.loads(audits[0]["payload"])["mode"] == "soft"

    # The abort-report doc persists and is readable back; metadata records soft.
    docs = _abort_report_docs(db)
    assert len(docs) >= 1
    assert json.loads(docs[-1]["metadata"])["mode"] == "soft"

    # DIRTY worktree was preserved: removal seam never invoked.
    assert removed_calls == []


def test_soft_abort_clean_worktree_removed_only_with_opt_in(workspace, monkeypatch):
    """SOFT path removes a CLEAN worktree ONLY when --clean-worktree is passed;
    without the flag a clean worktree is preserved."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: False)

    removed = []
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: removed.append(cwd) or True)

    # Without the opt-in flag: clean worktree preserved.
    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID]) == 0
    assert removed == []

    # With --clean-worktree: clean worktree removed.
    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--clean-worktree"]) == 0
    assert len(removed) == 1


# ── HARD abort ──────────────────────────────────────────────────────────────


def test_hard_abort_closes_team_and_removes_clean_worktree(workspace, monkeypatch):
    """HARD abort: teams.status -> 'closed', the abort-report doc persists, and
    a CLEAN worktree is auto-removed (--clean semantics on the hard path).
    main() returns 0."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: False)
    removed = []
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: removed.append(cwd) or True)

    rc = abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--hard"])
    assert rc == 0

    db = workspace["db"]
    # Hard target status.
    assert _team_status(db, TEAM_ID) == "closed"

    # Report persists, recorded with mode='hard'.
    docs = _abort_report_docs(db)
    assert len(docs) >= 1
    assert json.loads(docs[-1]["metadata"])["mode"] == "hard"

    # Shared core still ran: one 'aborted' audit event.
    assert len(backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")) == 1

    # Clean worktree auto-removed on the hard path.
    assert len(removed) == 1


def test_hard_abort_report_written_before_teardown(workspace, monkeypatch):
    """HARD writes the report FIRST (before teardown) so the postmortem
    survives even if a later step fails. We assert the report-first ORDER by
    making `_set_team_status` raise: the report must already be on disk when the
    teardown step blows up. main() does NOT swallow the failure (it propagates),
    but the durable report is what matters here."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: False)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    # Sabotage the teardown step that runs AFTER the report-first write.
    def _boom(*a, **k):
        raise RuntimeError("teardown step exploded")

    monkeypatch.setattr(abort, "_set_team_status", _boom)

    with pytest.raises(RuntimeError, match="teardown step exploded"):
        abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--hard"])

    # The report-first write landed BEFORE the teardown exploded.
    docs = _abort_report_docs(workspace["db"])
    assert len(docs) == 1
    assert json.loads(docs[0]["metadata"])["mode"] == "hard"


def test_hard_happy_path_links_final_report_to_crash_survival_copy(workspace, monkeypatch):
    """On the HARD happy path abort writes TWO postmortem docs — a report-first
    crash-survival copy and a final ACTUAL-outcomes copy — and the final one
    EXPLICITLY supersede-links the first via metadata.supersedes (mirroring
    write_spec_amendment's {version, supersedes}) so the two rows are linked,
    not silent duplicates.

    Exact-count: precisely 2 abort docs on the hard happy path, ordered
    (planned, actual). The SECOND carries metadata.supersedes == the FIRST's id.

    ANTI-REVERT: drop the supersedes link and the final doc has no supersedes
    key — this FAILS; collapse to one write and the count != 2 — this FAILS.
    """
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: False)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--hard"]) == 0

    docs = _abort_report_docs(workspace["db"])
    # Exactly two: the report-first crash-survival doc + the final actuals doc.
    assert len(docs) == 2, f"expected 2 hard-path abort docs, got {len(docs)}"
    first, final = docs[0], docs[1]
    first_meta = json.loads(first["metadata"])
    final_meta = json.loads(final["metadata"])
    # The first doc is the crash-survival (planned) copy: no supersedes link.
    assert "supersedes" not in first_meta
    # The final doc supersede-links the first by its id (explicit chain).
    assert final_meta.get("supersedes") == first["id"]


def test_hard_abort_dirty_worktree_preserved(workspace, monkeypatch):
    """Even on the forced HARD path a DIRTY worktree is PRESERVED — abort never
    destroys uncommitted work."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    removed = []
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: removed.append(cwd) or True)

    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--hard"]) == 0
    assert _team_status(workspace["db"], TEAM_ID) == "closed"
    # Dirty -> the removal seam was never called.
    assert removed == []


# ── AC-CRITICAL: doc persists on BOTH paths ─────────────────────────────────


@pytest.mark.parametrize(
    "argv, expected_status, expected_mode",
    [
        (["--team-pk", TEAM_PK, "--team-id", TEAM_ID], "shutting_down", "soft"),
        (["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--hard"], "closed", "hard"),
    ],
    ids=["soft", "hard"],
)
def test_abort_report_persists_both_paths(
    workspace, monkeypatch, argv, expected_status, expected_mode
):
    """AC#2 (the most-tested invariant): the abort-report doc PERSISTS on BOTH
    the soft AND the hard path, readable back via the backend / project_documents
    with domain='postmortem', subdomain='abort'. The doc's metadata records the
    correct mode, and teams.status reflects the path. main() returns 0."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)  # preserve
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    assert abort.main(argv) == 0

    db = workspace["db"]
    assert _team_status(db, TEAM_ID) == expected_status

    # Readable back through the BACKEND read surface (not just raw SQL) —
    # find_documents queries the same project_documents store abort.py writes.
    found = backend.find_documents(query="", domain="postmortem", subdomain="abort")
    assert len(found) >= 1
    modes = {json.loads(d["metadata"])["mode"] for d in found if d.get("metadata")}
    assert expected_mode in modes

    # And via the direct project_documents readback, exact (domain, subdomain).
    docs = _abort_report_docs(db)
    assert all(d["domain"] == "postmortem" and d["subdomain"] == "abort" for d in docs)
    assert any(json.loads(d["metadata"])["mode"] == expected_mode for d in docs)


def test_doc_persistence_is_non_vacuous(workspace, monkeypatch):
    """Non-vacuity guard: this test FAILS if the `write_document` call is
    removed from the implementation. We patch `backend.write_document` to count
    invocations and confirm abort.py actually calls it (a soft abort must
    produce at least one postmortem/abort doc). If the implementer deletes the
    write_document call, `calls` stays empty and this assertion fails."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    calls = []
    real_write_document = backend.write_document

    def _spy(**kwargs):
        calls.append(kwargs)
        return real_write_document(**kwargs)

    monkeypatch.setattr(abort.backend, "write_document", _spy)

    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID]) == 0

    # write_document was invoked with the postmortem/abort doc — the AC#2 write.
    abort_writes = [
        c for c in calls if c.get("domain") == "postmortem" and c.get("subdomain") == "abort"
    ]
    assert len(abort_writes) >= 1, (
        "abort.main did not call backend.write_document(domain='postmortem', "
        "subdomain='abort') — the abort-report write was removed."
    )
    # And the doc genuinely landed in the store (the write was not a no-op).
    assert len(_abort_report_docs(workspace["db"])) >= 1


# ── non-local mode: state mutations skipped, report best-effort ─────────────


def test_non_local_mode_skips_state_mutations_and_returns_zero(workspace, monkeypatch):
    """In NON-local mode abort SKIPS every Local-only state mutation
    (teams.status / 'aborted' audit) and returns 0.

    The migration-006 dispatch-state mutators are Local-mode-only, so a
    non-local abort must not flip teams.status and must not write an 'aborted'
    audit event. The pre-seeded teams row stays 'active' and no new audit rows
    appear.

    ANTI-REVERT: if the non-local guard is removed and abort runs the full
    teardown in Memex mode, teams.status would flip and an 'aborted' audit event
    would be written — this FAILS.
    """
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    rc = abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID])
    assert rc == 0

    db = workspace["db"]
    # No Local-only state mutation happened.
    assert _team_status(db, TEAM_ID) == "active"
    assert backend.list_team_audit(team_id=TEAM_ID, event_type="aborted") == []


def test_non_local_mode_abort_report_persists(workspace, monkeypatch):
    """Pin the atelier#90 part-3 behavior: the workspace-less abort-report
    doc now PERSISTS in non-local (Memex) mode via the §6.7 `_no-workspace_`
    key — the NotImplementedError gate is gone. abort.main still returns 0,
    and the report write is reached (it is no longer swallowed to None).

    Crucially this drives the REAL facade (`backend.write_document`) — only
    the leaf Memex-Core seams are stubbed via the canonical hermetic stub
    set — so BEFORE the fix the facade gate raises NotImplementedError and
    NO doc is captured (RED), and AFTER the fix the workspace-less write
    lands under the §6.7 `_no-workspace_/(no-project)/postmortem/` key
    (GREEN). State mutations (teams.status / 'aborted' audit) stay Local-only
    skips in non-local mode — only the REPORT write crosses the facade now.
    """
    from scripts import backend_memex

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    # Canonical hermetic Memex-Core stub set: capture librarian_output so we
    # can assert the §6.7 key, and short-circuit the index.documents seq
    # scan (which needs ~/.memex/config.json). The genuine workspace-less
    # branch must NOT reach the singleton fallback, so ban it.
    captured: dict = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": "x",
                "key": k["librarian_output"]["key"],
                "row_id": 1,
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(
        backend_memex,
        "_singleton_workspace",
        lambda: (_ for _ in ()).throw(
            AssertionError("workspace-less abort-report reached singleton fallback")
        ),
    )

    # The workspace-less write now lands; abort still returns 0.
    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID]) == 0

    # The abort-report write was REACHED and persisted (not swallowed) —
    # workspace-less postmortem doc under the §6.7 reserved key.
    assert captured, "abort-report write never reached the Memex backend (gate still firing?)"
    key = captured["librarian_output"]["key"]
    assert key.startswith("_no-workspace_/(no-project)/postmortem/"), key
    assert captured["payload"]["workspace_id"] is None
    assert captured["payload"]["project_id"] is None


# ── team_id handling ─────────────────────────────────────────────────────────


def test_explicit_team_id_drives_status_and_audit(workspace, monkeypatch):
    """An explicit --team-id is the sole source of team_id: the status mutation
    + 'aborted' audit event target the passed team_id."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    assert abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID]) == 0
    assert _team_status(workspace["db"], TEAM_ID) == "shutting_down"
    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    assert len(audits) == 1


def test_abort_with_missing_teams_row(workspace, monkeypatch):
    """An explicit --team-id that names NO row in `teams` makes the 'aborted'
    team_audit_log write FK-fail (team_audit_log.team_id REFERENCES teams,
    PRAGMA foreign_keys=ON in backend_local._conn) with sqlite3.IntegrityError.
    abort.py catches that as BEST-EFFORT (teardown bookkeeping already
    succeeded) and continues, so:

      (1) abort.main still returns 0 (no exception propagates),
      (2) the durable abort-report doc is still written (AC#2),
      (3) the audit-write failure is handled GRACEFULLY — the report's
          "what was torn down" records the 'aborted' event was SKIPPED because
          the audit write failed, and NO 'aborted' audit row actually lands.

    This pins the IntegrityError-catch branch in `_do_abort` (the production
    `teams` table is never populated, so this FK miss is the real-world path).

    NON-VACUITY: this test reaches the try/except by passing an explicit
    --team-id that misses `teams` (NOT None — None short-circuits to the
    `team_id is None` skip branch BEFORE the write is attempted, a different
    code path). If the IntegrityError catch were removed, the unhandled
    sqlite3.IntegrityError would propagate out of abort.main and BOTH the
    `rc == 0` assertion AND the "report still written" assertion (the final
    report is written AFTER the audit step) would go RED.
    """
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)  # preserve
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    missing_team_id = "team-does-not-exist"
    # Sanity: the chosen team_id is genuinely absent from `teams`, so the FK
    # constraint will fire (the catch branch is reached, not bypassed).
    assert _team_status(workspace["db"], missing_team_id) is None

    # Spy on write_document so we can read the final report's BODY markdown (the
    # torn_down note lives in the body, which is written to disk via `filename`,
    # not into a project_documents column) while still landing the real doc.
    bodies: list[str] = []
    real_write_document = backend.write_document

    def _spy(**kwargs):
        if kwargs.get("domain") == "postmortem" and kwargs.get("subdomain") == "abort":
            bodies.append(kwargs["body"])
        return real_write_document(**kwargs)

    monkeypatch.setattr(abort.backend, "write_document", _spy)

    rc = abort.main(["--team-pk", TEAM_PK, "--team-id", missing_team_id])
    # (1) Exit 0 — the IntegrityError was caught, nothing propagated.
    assert rc == 0

    db = workspace["db"]

    # (3a) No 'aborted' audit row landed for the bogus team_id — the FK-failing
    # write was swallowed, not retried/forced.
    assert backend.list_team_audit(team_id=missing_team_id, event_type="aborted") == []
    # And the seeded (real) team got no spurious audit event either.
    assert backend.list_team_audit(team_id=TEAM_ID, event_type="aborted") == []

    # (2) The durable abort-report doc STILL persisted (AC#2 holds even though
    # the audit write FK-failed). Readable back via the project_documents store.
    docs = _abort_report_docs(db)
    assert len(docs) >= 1
    final_meta = json.loads(docs[-1]["metadata"])
    assert final_meta["mode"] == "soft"
    assert final_meta["team_id"] == missing_team_id

    # (3b) The audit-write failure was handled GRACEFULLY: the final report's
    # body records the 'aborted' event was SKIPPED due to the failed audit write
    # (the IntegrityError-catch branch's torn_down note), not silently dropped.
    assert bodies, "abort-report write_document was never called"
    final_body = bodies[-1]
    assert "team_audit_log: 'aborted' event SKIPPED" in final_body
    assert "audit write failed" in final_body


def test_missing_team_id_skips_audit_but_still_reports(workspace, monkeypatch):
    """With no --team-id the abort still completes (exit 0), flips teams.status
    only for a resolvable team (here it is a no-op — no team_id to target), and
    SKIPS the 'aborted' audit event (team_audit_log.team_id FKs teams). The
    abort-report doc still persists."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)
    monkeypatch.setattr(abort, "_remove_worktree", lambda cwd: True)

    assert abort.main(["--team-pk", TEAM_PK]) == 0

    db = workspace["db"]
    # No team_id → teams.status untouched (the seeded team stays 'active') and no
    # 'aborted' audit event written.
    assert _team_status(db, TEAM_ID) == "active"
    assert backend.list_team_audit(team_id=TEAM_ID, event_type="aborted") == []
    # But the report is still durable.
    assert len(_abort_report_docs(db)) >= 1


# ── #66 resume hooks: --project-id / --phase fold into payload + metadata ────


def _live_task(db_path, project_id_text, team_pk):
    """Seed ONE non-terminal task scoped to team_pk so resume.find_resumable_arc
    has a resumable signal after the abort round-trip. tasks.project_id FKs to
    projects.id (INTEGER), so we stand up a workspace+project row first and
    return nothing — the resumable signal is the non-terminal task's team_pk,
    which find_resumable_arc reads from the audit payload, not the project FK."""
    conn = sqlite3.connect(db_path)
    now = "2026-05-31T00:00:00Z"
    ws = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, created_at, updated_at) "
        "VALUES ('rt-ws', '/tmp/rt-ws', 'RT WS', ?, ?)",
        (now, now),
    )
    ws_id = ws.lastrowid
    proj = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, phase, created_by, created_at, updated_at) "
        "VALUES (?, 'rt-proj', 'RT Proj', 'design:open', 'pm', ?, ?)",
        (ws_id, now, now),
    )
    proj_rowid = proj.lastrowid
    conn.execute(
        "INSERT INTO tasks (project_id, title, status, created_by, created_at, updated_at, "
        "parallel_group, team_pk) VALUES (?, 'rt-live', 'pending', 'pm', ?, ?, 1, ?)",
        (proj_rowid, now, now, team_pk),
    )
    conn.commit()
    conn.close()


def test_abort_folds_project_id_and_phase_into_audit_payload_and_metadata(workspace):
    """#66: abort --project-id/--phase folds project_id + abort_phase +
    incomplete_task_ids into BOTH the 'aborted' audit payload AND the abort-report
    doc metadata, so resume.find_resumable_arc can detect the arc AND resume AT
    the abort phase without re-planning. ANTI-REVERT: dropping the fold from
    either sink leaves the payload/metadata without abort_phase and the
    assertions below go RED."""
    project_id = "proj-1"  # matches the workspace fixture's teams.project_id
    phase = "tdd:red"  # a REAL phase from the vocabulary (#66 N1 validation)

    rc = abort.main(
        [
            "--team-pk",
            TEAM_PK,
            "--team-id",
            TEAM_ID,
            "--project-id",
            project_id,
            "--phase",
            phase,
            "--clean-worktree",
        ]
    )
    assert rc == 0

    db = workspace["db"]

    # (a) The 'aborted' audit payload carries project_id + abort_phase.
    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    assert len(audits) == 1
    payload = json.loads(audits[0]["payload"])
    assert payload["project_id"] == project_id
    assert payload["abort_phase"] == phase
    assert "incomplete_task_ids" in payload

    # (b) The abort-report doc metadata ALSO carries them (the final report).
    docs = _abort_report_docs(db)
    assert len(docs) >= 1
    meta = json.loads(docs[-1]["metadata"])
    assert meta["project_id"] == project_id
    assert meta["abort_phase"] == phase


def test_abort_payload_round_trips_to_find_resumable_arc(workspace):
    """The folded payload round-trips: after a soft abort with --project-id/--phase
    and a non-terminal task scoped to the team_pk, resume.find_resumable_arc
    detects the arc and surfaces the abort_phase read from the audit payload."""
    from scripts import resume

    project_id = "proj-1"
    phase = "tdd:red"  # a REAL phase from the vocabulary (#66 N1 validation)
    _live_task(workspace["db"], project_id, TEAM_PK)

    assert (
        abort.main(
            [
                "--team-pk",
                TEAM_PK,
                "--team-id",
                TEAM_ID,
                "--project-id",
                project_id,
                "--phase",
                phase,
                "--clean-worktree",
            ]
        )
        == 0
    )

    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.team_id == TEAM_ID
    assert offer.project_id == project_id
    assert offer.abort_phase == phase
    assert offer.incomplete_count == 1


def test_abort_without_resume_flags_keeps_payload_keys_none(workspace):
    """Back-compat: omitting --project-id/--phase folds the keys as None (the
    payload/metadata schema stays stable) and find_resumable_arc still detects
    the arc via the audit join — abort_phase is simply None for a legacy abort."""
    abort.main(["--team-pk", TEAM_PK, "--team-id", TEAM_ID, "--clean-worktree"])
    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    payload = json.loads(audits[0]["payload"])
    # Keys present, defaulted to None — a stable schema for resume to read.
    assert payload["project_id"] is None
    assert payload["abort_phase"] is None


# ── #66 N1: --phase validation against the canonical phase vocabulary ────────


def test_unknown_phase_warns_and_records_none(workspace, monkeypatch, capsys):
    """#66 N1 ROBUSTNESS: a --phase value NOT in the phase vocabulary is REJECTED
    — abort logs a clear WARN to stderr and records abort_phase=None so the bogus
    phase never round-trips into projects.phase on a resume-continue. The abort
    is RESILIENT (does NOT hard-fail): main() still returns 0 and the rest of the
    teardown (status / team_delete / audit) runs. ANTI-REVERT: dropping the
    validation lets 'no-such-phase:typo' land in the payload and this goes RED."""
    monkeypatch.setattr(abort, "_worktree_is_dirty", lambda cwd: True)

    rc = abort.main(
        [
            "--team-pk",
            TEAM_PK,
            "--team-id",
            TEAM_ID,
            "--phase",
            "no-such-phase:typo",
        ]
    )
    # Resilience: the abort still completes (exit 0), it does not hard-fail.
    assert rc == 0

    # A clear WARN names the offending phase.
    err = capsys.readouterr().err
    assert "no-such-phase:typo" in err
    assert "not a known phase" in err

    # The bogus phase is NOT propagated into the audit payload — recorded as None.
    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    assert len(audits) == 1
    payload = json.loads(audits[0]["payload"])
    assert payload["abort_phase"] is None
    # And not into the abort-report metadata either.
    docs = _abort_report_docs(workspace["db"])
    assert json.loads(docs[-1]["metadata"])["abort_phase"] is None


def test_valid_phase_is_recorded_as_is(workspace):
    """#66 N1: a --phase value that IS in the canonical vocabulary (e.g. the
    schema-seeded 'review:open') is accepted verbatim and folded into the audit
    payload + report metadata, unchanged."""
    rc = abort.main(
        [
            "--team-pk",
            TEAM_PK,
            "--team-id",
            TEAM_ID,
            "--phase",
            "review:open",
            "--clean-worktree",
        ]
    )
    assert rc == 0

    audits = backend.list_team_audit(team_id=TEAM_ID, event_type="aborted")
    assert json.loads(audits[0]["payload"])["abort_phase"] == "review:open"
    docs = _abort_report_docs(workspace["db"])
    assert json.loads(docs[-1]["metadata"])["abort_phase"] == "review:open"
