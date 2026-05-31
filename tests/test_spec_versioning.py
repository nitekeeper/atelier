"""atelier#62 Part C DECISION 2 — spec versioning + the metadata column.

Covers:

* The new `project_documents.metadata` TEXT column (migration
  007_project_documents_metadata.sql) exists after migrations and
  round-trips JSON through `backend_local.write_document` /
  `backend_local.get_document`.
* `documents.write_spec_amendment` creates a NEW doc row (the prior row is
  still readable and is NOT mutated in place), bumps `metadata.version`
  (1 -> 2 -> 3 across repeated amendments), and sets `metadata.supersedes`
  to the prior doc id.
* `write_spec_amendment` refuses a non-spec doc (the §6.4 domain=project /
  subdomain=spec gate) and a missing prior doc.

Mirrors the Local-mode DB fixture style of
`tests/test_backend_local_documents.py` + `tests/test_documents.py`: a
cwd-rooted fake workspace with `.git`, a migrated `.ai/atelier.db`, and
`detect_mode` forced to "local" so writes route through `backend_local`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backend, backend_local
from scripts.documents import get_document, write_spec_amendment
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_minimum(db_path: str) -> tuple[int, int]:
    """Seed workspaces + roles + agents + projects for the v1.1.0 schema.

    Returns (workspace_id, project_id). Same shape as the helper in
    test_backend_local_documents.py.
    """
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", "test workspace", now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Product Manager", "PM", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("atelier-pm-1", "PM", role_id, "pm", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth Service", "OAuth2 service", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ws_id, proj_id


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Local-mode workspace: cwd-rooted .git + migrated atelier.db, detect_mode
    forced to 'local' so backend routes through backend_local."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ws_id, proj_id = _seed_minimum(str(db))
    return {"root": root, "db": str(db), "workspace_id": ws_id, "project_id": proj_id}


def _make_spec(workspace, title="Auth Spec", body="# Goal\n\nv1 spec body."):
    """Create a v1 spec row (domain=project, subdomain=spec) via the facade.

    Returns the new doc id. Goes through `backend.write_document` (NOT a
    direct backend_local call) so the test exercises the real facade path.
    """
    result = backend.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="project",
        subdomain="spec",
        title=title,
        body=body,
        metadata={},
        caller_agent_id="atelier-pm-1",
    )
    return result["row_id"]


# ── migration 007: the metadata column exists + round-trips ─────────────────


def test_metadata_column_present_after_migrations(workspace):
    """Migration 007 adds project_documents.metadata (TEXT)."""
    conn = sqlite3.connect(workspace["db"])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(project_documents)").fetchall()]
    conn.close()
    assert "metadata" in cols, "migration 007 did not add the metadata column"


def test_write_document_round_trips_metadata_json(workspace):
    """backend_local.write_document persists metadata as JSON and
    get_document reads it back — the recon-noted 'Local drops metadata' bug
    is fixed."""
    meta = {"version": 7, "supersedes": 123, "note": "round-trip"}
    r = backend_local.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="project",
        subdomain="spec",
        title="Meta Doc",
        body="body",
        metadata=meta,
        caller_agent_id="atelier-pm-1",
    )
    row = backend_local.get_document(doc_id=r["row_id"])
    assert row is not None
    assert json.loads(row["metadata"]) == meta


def test_write_document_none_metadata_stores_null(workspace):
    """A plain create (empty/None metadata) stores SQL NULL — back-compat:
    no `{}` literal cluttering the column."""
    r = backend_local.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="design",
        subdomain=None,
        title="Plain Doc",
        body="body",
        metadata={},
        caller_agent_id="atelier-pm-1",
    )
    row = backend_local.get_document(doc_id=r["row_id"])
    assert row is not None
    assert row["metadata"] is None


# ── write_spec_amendment: new row, version bump, supersedes, prior preserved ─


def test_amendment_creates_new_row_prior_preserved(workspace):
    """An amendment creates a NEW doc id; the prior row is still readable and
    its body/metadata are NOT mutated in place."""
    prior_id = _make_spec(workspace, title="Auth Spec", body="# Goal\n\nv1 body.")
    prior_before = get_document(workspace["db"], prior_id)

    new_doc = write_spec_amendment(
        workspace["db"],
        prior_doc_id=prior_id,
        title="Auth Spec (v2)",
        body="# Goal\n\nv2 body — amended.",
        created_by="atelier-pm-1",
    )

    assert new_doc["id"] != prior_id, "amendment must create a NEW row, not update in place"

    # Prior row still readable and unchanged (NOT mutated in place).
    prior_after = get_document(workspace["db"], prior_id)
    assert prior_after is not None
    assert prior_after["title"] == prior_before["title"]
    assert prior_after["filename"] == prior_before["filename"]
    # The prior row carried no version metadata; it stays that way.
    assert prior_after["metadata"] is None


def test_amendment_metadata_version_and_supersedes(workspace):
    """metadata.version is prior+1 (default-1 => 2) and supersedes points at
    the prior doc id."""
    prior_id = _make_spec(workspace)
    new_doc = write_spec_amendment(
        workspace["db"],
        prior_doc_id=prior_id,
        title="Auth Spec (v2)",
        body="v2 body",
        created_by="atelier-pm-1",
    )
    assert new_doc["metadata"]["version"] == 2
    assert new_doc["metadata"]["supersedes"] == prior_id


def test_amendment_version_increments_across_chain(workspace):
    """1 -> 2 -> 3: each amendment reads the PRIOR row's version and bumps it,
    and each supersedes the immediately-prior doc.

    NON-VACUOUS: if write_spec_amendment hardcoded version=2 or mutated in
    place, the third row's version would not be 3 and the supersedes chain
    would not walk back through distinct ids — both assertions would fail."""
    v1 = _make_spec(workspace, title="Spec v1", body="b1")
    d2 = write_spec_amendment(
        workspace["db"], prior_doc_id=v1, title="Spec v2", body="b2", created_by="atelier-pm-1"
    )
    d3 = write_spec_amendment(
        workspace["db"],
        prior_doc_id=d2["id"],
        title="Spec v3",
        body="b3",
        created_by="atelier-pm-1",
    )
    assert d2["metadata"]["version"] == 2
    assert d3["metadata"]["version"] == 3
    assert d2["metadata"]["supersedes"] == v1
    assert d3["metadata"]["supersedes"] == d2["id"]
    # All three rows are distinct and all still readable (none mutated away).
    ids = {v1, d2["id"], d3["id"]}
    assert len(ids) == 3
    for doc_id in ids:
        assert get_document(workspace["db"], doc_id) is not None


# ── write_spec_amendment: refusals ──────────────────────────────────────────


def test_amendment_refuses_non_spec_doc(workspace):
    """A doc that is not stored at domain=project / subdomain=spec is refused
    (the §6.4 'clean way to tell' gate)."""
    # A design doc (domain=design), not a 9-section PM spec.
    result = backend.write_document(
        workspace_id=workspace["workspace_id"],
        project_id=workspace["project_id"],
        domain="design",
        subdomain="auth",
        title="Design Doc",
        body="not a spec",
        metadata={},
        caller_agent_id="atelier-pm-1",
    )
    with pytest.raises(ValueError, match="not a spec"):
        write_spec_amendment(
            workspace["db"],
            prior_doc_id=result["row_id"],
            title="Attempted amend",
            body="x",
            created_by="atelier-pm-1",
        )


def test_amendment_refuses_missing_prior(workspace):
    """Amending a non-existent doc id raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        write_spec_amendment(
            workspace["db"],
            prior_doc_id=999999,
            title="x",
            body="x",
            created_by="atelier-pm-1",
        )


# ── atelier#66 [S3] T5 — spec-versioning metadata round-trip in Memex (#62) ──
#
# test_spec_versioning is Local-only (its module docstring even cites a past
# "Local drops metadata" bug; grep-confirms ZERO 'memex' hits). The Memex path
# folds `metadata` into the Index blob rather than a wide column — the path most
# likely to silently drop the versioning fields (R4) — and was untested. This
# force-Memex round-trip pins that an amendment's `metadata.version` (1->2) and
# `metadata.supersedes` reach the Memex `write_document` payload. Hermetic via
# the canonical stub set: `backend_memex.get_document` returns the prior v1 spec
# (and None for the post-write re-read so `write_spec_amendment` returns its echo
# with synthesised metadata — no live ~/.memex), and `backend_memex.write_document`
# is spied for the payload assertion.


def test_spec_amendment_round_trips_metadata_memex(monkeypatch):
    """Force-Memex: amending a v1 spec issues `backend.write_document` whose
    folded metadata carries version=2 + supersedes=<prior_id>. Asserts at the
    `backend_memex.write_document` mock boundary (the facade folds the wide
    metadata into the Memex Index blob)."""
    from scripts import backend, backend_memex, mode_detector
    from scripts.documents import write_spec_amendment

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)

    PRIOR_ID = 7
    # The prior v1 spec the amendment reads (domain/subdomain must be the §6.4
    # spec coordinates or write_spec_amendment refuses it). metadata.version=1.
    prior_row = {
        "id": PRIOR_ID,
        "workspace_id": 1,
        "project_id": 3,
        "domain": "project",
        "subdomain": "spec",
        "title": "Auth Spec",
        "metadata": {"version": 1},
    }

    def fake_get_document(*, doc_id):
        # Prior read returns the v1 spec; the post-write re-read (new id) misses
        # so write_spec_amendment returns its echo with synthesised metadata.
        return prior_row if doc_id == PRIOR_ID else None

    monkeypatch.setattr(backend_memex, "get_document", fake_get_document)

    captured: dict = {}

    def fake_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 99, "index_id": "i-99"}

    monkeypatch.setattr(backend_memex, "write_document", fake_write_document)

    new_doc = write_spec_amendment(
        "ignored-db-path",  # Memex path never touches the local sqlite file
        prior_doc_id=PRIOR_ID,
        title="Auth Spec (v2)",
        body="# Goal\n\nv2 body.",
        created_by="atelier-pm-1",
    )

    # The Memex write was reached and the versioning metadata survived the fold
    # into the Index blob (NOT dropped — the R4 regression this pins).
    assert captured, "write_spec_amendment never reached the Memex backend"
    assert captured["metadata"]["version"] == 2
    assert captured["metadata"]["supersedes"] == PRIOR_ID
    # The spec coordinates ride through unchanged so the new row stays a spec.
    # domain is a native write_document param; subdomain is folded into the
    # Memex Index metadata blob (the facade's adapter pattern).
    assert captured["domain"] == "project"
    assert captured.get("subdomain") == "spec" or captured["metadata"].get("subdomain") == "spec"
    # The echoed return carries the bumped version for the caller.
    assert new_doc["metadata"]["version"] == 2
    assert new_doc["metadata"]["supersedes"] == PRIOR_ID
