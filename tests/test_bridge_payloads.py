# tests/test_bridge_payloads.py
"""Unit tests for scripts/bridge_payloads.py + migration 011.

Cycle-1 payload-referencing foundation (AI-0-store). Coverage:

* migration 011 leaves PRAGMA user_version at 1 (bridge wire pin intact)
* store→get byte-exact round-trip, including a multi-byte (non-ASCII) body
* content-address dedup: same body twice → one row, same ref
* sha256 + byte_len agree on the UTF-8 byte sequence
* oversize body rejected at the application-layer 1 MiB ceiling
* append-only: UPDATE and DELETE on bridge_payloads are refused
* body has NO 8 KiB cap (a >8 KiB body stores fine)
* team_id FK scope enforced (unknown team rejected)
* get miss → None; exists() reflects presence

Test DB built via scripts.migrate.apply_migrations so we run against the
real schema + triggers.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts import bridge_payloads
from scripts.bridge_payloads import (
    MAX_BODY_BYTES,
    PayloadTooLarge,
    compute_sha256,
    exists,
    get,
    store,
)
from scripts.migrate import apply_migrations

REPO_ROOT = Path(__file__).parent.parent
MIGRATIONS_SHARED = REPO_ROOT / "migrations" / "shared"


@pytest.fixture
def store_db(tmp_path: Path) -> str:
    """Fresh DB w/ shared migrations applied + team T1 seeded for the FK."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS_SHARED)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
        ("T1", "P1", "backend-engineer-1", "active"),
    )
    conn.commit()
    conn.close()
    return str(db)


def _open(db_path: str) -> sqlite3.Connection:
    return bridge_payloads._open_db(db_path)


def test_migration_011_keeps_user_version_at_1(store_db: str) -> None:
    """The bridge wire pin MUST stay 1 — bridge_send/bridge_read hard-fail
    on user_version != SCHEMA_VERSION(=1). Migration 011 is additive only."""
    conn = _open(store_db)
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    assert found == 1, f"migration 011 must NOT bump user_version; got {found}"


def test_store_get_round_trip_ascii(store_db: str) -> None:
    conn = _open(store_db)
    body = "hello world"
    ref = store(conn, "T1", body)
    assert get(conn, "T1", ref["sha256"]) == body


def test_store_get_round_trip_multibyte_byte_exact(store_db: str) -> None:
    """Non-ASCII body round-trips byte-for-byte; sha256/byte_len computed
    over the UTF-8 byte sequence, never codepoints."""
    conn = _open(store_db)
    body = "café ☕ 日本語 — </untrusted> 🚀"
    ref = store(conn, "T1", body)
    got = get(conn, "T1", ref["sha256"])
    assert got == body
    assert got.encode("utf-8") == body.encode("utf-8")
    assert ref["byte_len"] == len(body.encode("utf-8"))
    assert ref["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_sha256_and_byte_len_agree_with_db(store_db: str) -> None:
    conn = _open(store_db)
    body = "日本語テスト"
    ref = store(conn, "T1", body)
    row = conn.execute(
        "SELECT byte_len, length(CAST(body AS BLOB)) AS blob_len FROM bridge_payloads "
        "WHERE team_id=? AND sha256=?",
        ("T1", ref["sha256"]),
    ).fetchone()
    assert row[0] == row[1] == len(body.encode("utf-8"))
    assert compute_sha256(body) == ref["sha256"]


def test_dedup_same_body_one_row_same_ref(store_db: str) -> None:
    """Content-addressed: storing the same body twice is an idempotent
    no-op returning the identical ref. This is bridge_send's replay-safety."""
    conn = _open(store_db)
    body = "a" * 9000
    ref1 = store(conn, "T1", body)
    ref2 = store(conn, "T1", body)
    assert ref1 == ref2
    count = conn.execute(
        "SELECT COUNT(*) FROM bridge_payloads WHERE team_id=? AND sha256=?",
        ("T1", ref1["sha256"]),
    ).fetchone()[0]
    assert count == 1


def test_body_has_no_8kib_cap(store_db: str) -> None:
    """The whole point of the store: a body larger than the 8 KiB wire cap
    persists fine (bridge_messages.payload's CHECK does not apply here)."""
    conn = _open(store_db)
    body = "x" * 20000  # > 8192
    ref = store(conn, "T1", body)
    assert ref["byte_len"] == 20000
    assert get(conn, "T1", ref["sha256"]) == body


def test_oversize_body_rejected_at_ceiling(store_db: str) -> None:
    conn = _open(store_db)
    body = "x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(PayloadTooLarge):
        store(conn, "T1", body)


def test_append_only_no_update(store_db: str) -> None:
    conn = _open(store_db)
    ref = store(conn, "T1", "immutable")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE bridge_payloads SET body='tampered' WHERE team_id=? AND sha256=?",
            ("T1", ref["sha256"]),
        )


def test_append_only_no_delete(store_db: str) -> None:
    conn = _open(store_db)
    ref = store(conn, "T1", "durable")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM bridge_payloads WHERE team_id=? AND sha256=?",
            ("T1", ref["sha256"]),
        )


def test_unknown_team_rejected_by_fk(store_db: str) -> None:
    conn = _open(store_db)
    with pytest.raises(sqlite3.IntegrityError):
        store(conn, "NO_SUCH_TEAM", "orphan")


def test_get_miss_returns_none_and_exists(store_db: str) -> None:
    conn = _open(store_db)
    assert get(conn, "T1", "deadbeef") is None
    assert exists(conn, "T1", "deadbeef") is False
    ref = store(conn, "T1", "present")
    assert exists(conn, "T1", ref["sha256"]) is True
