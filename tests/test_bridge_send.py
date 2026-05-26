# tests/test_bridge_send.py
"""Unit tests for scripts/bridge_send.py (epic #37, team-mode bridge).

Coverage targets (per Phase-4 wave-2 ack):

* idempotency replay returns the *original* seq + persona_snapshot_id
  (equality, not just non-error)
* payload > 8 KiB raises PayloadTooLargeError before any lock taken
* DB whose PRAGMA user_version != 1 raises SchemaVersionMismatch
* sender token missing → SenderAuthError; --allow-unsafe-sender + --from
  is the only test-mode escape hatch

The test DB is built by applying migrations/shared/ to a tmp_path
SQLite file via scripts.migrate.apply_migrations — same path the real
runtime uses, so we exercise the actual schema and triggers (no fixture
divergence). Sender authentication is exercised end-to-end against the
HMAC token format dispatch.py will use.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import bridge_send
from scripts.bridge_send import (
    PAYLOAD_MAX_BYTES,
    SCHEMA_VERSION,
    PayloadTooLargeError,
    SchemaVersionMismatch,
    SenderAuthError,
    send,
)
from scripts.migrate import apply_migrations

REPO_ROOT = Path(__file__).parent.parent
MIGRATIONS_SHARED = REPO_ROOT / "migrations" / "shared"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def team_db(tmp_path: Path) -> str:
    """Fresh DB with migrations/shared/ applied + one team/member seeded.

    Returns the DB path. The seeded team:
        team_id = "T1", lead = "backend-engineer-1"
        member  = role_id "backend-engineer-1", persona_snapshot id=1
        member  = role_id "team-lead",          persona_snapshot id=1
    so both can act as sender or recipient.
    """
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS_SHARED)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO persona_snapshots (persona_version, persona_blob) VALUES (?, ?)",
        ("v1", "{}"),
    )
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
        ("T1", "P1", "backend-engineer-1", "active"),
    )
    conn.execute(
        "INSERT INTO team_members (team_id, role_id, member_name, "
        "persona_snapshot_id) VALUES (?, ?, ?, ?)",
        ("T1", "backend-engineer-1", "backend-engineer-1", 1),
    )
    conn.execute(
        "INSERT INTO team_members (team_id, role_id, member_name, "
        "persona_snapshot_id) VALUES (?, ?, ?, ?)",
        ("T1", "team-lead", "team-lead", 1),
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def clone_root_with_token(tmp_path: Path) -> Path:
    """Provision .atelier/team/T1/{.secret, backend-engineer-1.token}.

    Tokens are computed the same way dispatch.py will compute them:
    HMAC-SHA256(secret, role_id) hex. Files written 0600 so the
    permission posture mirrors production.
    """
    tdir = tmp_path / ".atelier" / "team" / "T1"
    tdir.mkdir(parents=True)
    secret = b"deadbeef-team-secret-32-bytes-min"
    (tdir / ".secret").write_bytes(secret)
    os.chmod(tdir / ".secret", 0o600)
    role = "backend-engineer-1"
    tok = hmac.new(secret, role.encode(), sha256).hexdigest()
    (tdir / f"{role}.token").write_text(tok)
    os.chmod(tdir / f"{role}.token", 0o600)
    return tmp_path


# ── Tests: core writer ─────────────────────────────────────────────────────


def test_send_allocates_monotonic_seq_per_recipient(team_db: str) -> None:
    """seq starts at 1 and increments per (team_id, recipient)."""
    r1 = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload="first",
    )
    r2 = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload="second",
    )
    assert r1["seq"] == 1
    assert r2["seq"] == 2
    assert r1["deduped"] is False
    assert r2["deduped"] is False
    assert r1["persona_snapshot_id"] == 1


def test_send_seq_is_independent_per_recipient(team_db: str) -> None:
    """Per-recipient FIFO: two recipients each start at seq=1."""
    a = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload="x",
    )
    b = send(
        team_db,
        team_id="T1",
        recipient="backend-engineer-1",
        sender_id="team-lead",
        kind="reply",
        payload="y",
    )
    assert a["seq"] == 1
    assert b["seq"] == 1


# ── Tests: idempotency replay ──────────────────────────────────────────────


def test_idempotency_replay_returns_original_seq(team_db: str) -> None:
    """Replay with the same ULID returns the original seq AND original
    persona_snapshot_id (equality, not just non-error)."""
    ulid = "01H8XGJWBWBAJ1ABCDEFGHIJKL"  # 26 chars
    assert len(ulid) == 26

    first = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload="payload-A",
        idempotency_key=ulid,
    )
    # A second send with the SAME key but a *different* payload must
    # NOT write a new row, and MUST return the prior seq + snapshot id.
    second = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload="payload-B-IGNORED",
        idempotency_key=ulid,
    )
    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["seq"] == first["seq"]
    assert second["persona_snapshot_id"] == first["persona_snapshot_id"]

    # And the log must still contain only the original payload (proof
    # the dedupe path did not silently overwrite).
    conn = sqlite3.connect(team_db)
    try:
        rows = conn.execute(
            "SELECT payload FROM bridge_messages WHERE idempotency_key=?",
            (ulid,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "payload-A"


# ── Tests: payload ceiling ─────────────────────────────────────────────────


def test_oversize_payload_rejected_at_load(team_db: str) -> None:
    """Payload > 8 KiB raises PayloadTooLargeError at the writer layer.

    Routed through ``_load_payload`` so the CLI's @file path shares the
    same enforcement. (L16: renamed from the old ``_rejected_before_lock``
    name — this test exercises ``_load_payload`` only and proves nothing
    about the DB lock; the at-load name is what's actually verified.)
    """
    big = "x" * (PAYLOAD_MAX_BYTES + 1)
    with pytest.raises(PayloadTooLargeError):
        bridge_send._load_payload(big)


def test_payload_exactly_at_limit_accepted(team_db: str) -> None:
    """Boundary check: 8192 bytes exactly is fine."""
    edge = "y" * PAYLOAD_MAX_BYTES
    assert bridge_send._load_payload(edge) == edge


# ── Tests: SCHEMA_VERSION pin ──────────────────────────────────────────────


def test_schema_version_mismatch_hard_fails(tmp_path: Path) -> None:
    """A DB whose user_version != SCHEMA_VERSION raises explicitly,
    even if the bridge_messages table happens to exist."""
    db = tmp_path / "bad.db"
    apply_migrations(str(db), MIGRATIONS_SHARED)
    # Force a mismatch by writing a deliberately-wrong user_version.
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 99}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionMismatch) as exc:
        send(
            str(db),
            team_id="T1",
            recipient="team-lead",
            sender_id="backend-engineer-1",
            kind="reply",
            payload="never gets written",
        )
    # The message must name both versions so the operator can diagnose
    # without re-reading the source.
    assert str(SCHEMA_VERSION) in str(exc.value)
    assert str(SCHEMA_VERSION + 99) in str(exc.value)


# ── Tests: sender token authentication ─────────────────────────────────────


def test_sender_token_missing_rejected(
    tmp_path: Path,
    team_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token file on disk → SenderAuthError, even with the env set."""
    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", "backend-engineer-1")
    monkeypatch.delenv("ATELIER_TEAM_SECRET", raising=False)
    # clone_root is empty — no .atelier/team/ directory exists.
    argv = [
        "--team",
        "T1",
        "--to",
        "team-lead",
        "--kind",
        "reply",
        "--payload",
        "should-never-land",
        "--db",
        team_db,
        "--clone-root",
        str(tmp_path),
    ]
    rc = bridge_send.main(argv)
    assert rc == 2  # SenderAuthError → exit 2


def test_env_unset_rejects_without_unsafe_flag(
    tmp_path: Path,
    team_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ATELIER_TEAM_SELF_ROLE absent → SenderAuthError naming the env."""
    monkeypatch.delenv("ATELIER_TEAM_SELF_ROLE", raising=False)
    with pytest.raises(SenderAuthError) as exc:
        bridge_send._resolve_sender(
            tmp_path,
            "T1",
            allow_unsafe=False,
            unsafe_from=None,
        )
    assert "ATELIER_TEAM_SELF_ROLE" in str(exc.value)


def test_valid_token_accepted_end_to_end(
    team_db: str,
    clone_root_with_token: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path through the CLI: env + valid HMAC token + DB write."""
    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", "backend-engineer-1")
    argv = [
        "--team",
        "T1",
        "--to",
        "team-lead",
        "--kind",
        "reply",
        "--payload",
        "hello",
        "--db",
        team_db,
        "--clone-root",
        str(clone_root_with_token),
    ]
    rc = bridge_send.main(argv)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seq"] == 1
    assert out["deduped"] is False
    assert out["persona_snapshot_id"] == 1


def test_tampered_token_rejected(
    team_db: str,
    clone_root_with_token: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overwrite the token with garbage → SenderAuthError on HMAC compare."""
    tok_path = clone_root_with_token / ".atelier" / "team" / "T1" / "backend-engineer-1.token"
    tok_path.write_text("0" * 64)  # valid-looking hex, wrong HMAC
    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", "backend-engineer-1")
    with pytest.raises(SenderAuthError) as exc:
        bridge_send._resolve_sender(
            clone_root_with_token,
            "T1",
            allow_unsafe=False,
            unsafe_from=None,
        )
    assert "HMAC mismatch" in str(exc.value)


def test_allow_unsafe_sender_requires_from() -> None:
    """--allow-unsafe-sender without --from refuses to invent identity."""
    with pytest.raises(SenderAuthError):
        bridge_send._resolve_sender(
            Path("/tmp"),
            "T1",
            allow_unsafe=True,
            unsafe_from=None,
        )


def test_allow_unsafe_sender_with_from_bypasses_token(
    team_db: str,
    tmp_path: Path,
) -> None:
    """--allow-unsafe-sender + --from writes successfully with no token."""
    sender = bridge_send._resolve_sender(
        tmp_path,
        "T1",
        allow_unsafe=True,
        unsafe_from="backend-engineer-1",
    )
    assert sender == "backend-engineer-1"
    r = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id=sender,
        kind="reply",
        payload="unsafe-path-ok-for-tests",
    )
    assert r["seq"] == 1


# ── Tests: CLI surface ─────────────────────────────────────────────────────


def test_cli_payload_file_loader(team_db: str, tmp_path: Path) -> None:
    """--payload @path reads from a file, honoring the size cap."""
    p = tmp_path / "msg.txt"
    p.write_text("from-a-file")
    assert bridge_send._load_payload(f"@{p}") == "from-a-file"


def test_cli_bad_idem_length_rejected(team_db: str) -> None:
    """Non-26-char idempotency key surfaces as a BridgeSendError."""
    with pytest.raises(bridge_send.BridgeSendError):
        send(
            team_db,
            team_id="T1",
            recipient="team-lead",
            sender_id="backend-engineer-1",
            kind="reply",
            payload="x",
            idempotency_key="too-short",
        )


def test_cli_bad_kind_rejected(team_db: str) -> None:
    """Unknown kind → BridgeSendError before any DB work."""
    with pytest.raises(bridge_send.BridgeSendError):
        send(
            team_db,
            team_id="T1",
            recipient="team-lead",
            sender_id="backend-engineer-1",
            kind="not-a-real-kind",
            payload="x",
        )


# ── Tests: H5 multi-byte + at-cap payload byte-counting ────────────────────


def test_multibyte_payload_byte_cap(team_db: str) -> None:
    """CJK chars count as 3 UTF-8 bytes — writer + schema must agree.

    4097 copies of '界' is 12291 bytes (4097 * 3) — well over the 8192
    byte cap. The writer-side gate (PayloadTooLargeError) and the
    schema-side CHECK constraint must BOTH reject the same input, so a
    bypass of one is still caught by the other.
    """
    payload = "界" * 4097
    assert len(payload.encode("utf-8")) == 4097 * 3  # 12291 bytes

    # Writer-side gate.
    with pytest.raises(PayloadTooLargeError):
        send(
            team_db,
            team_id="T1",
            recipient="team-lead",
            sender_id="backend-engineer-1",
            kind="reply",
            payload=payload,
        )

    # Schema-side CHECK constraint — bypass the writer and INSERT raw.
    # The CHECK on bridge_messages.payload (length(CAST(payload AS BLOB))
    # <= 8192) must reject the same input the writer did, so the two
    # gates can't drift.
    conn = sqlite3.connect(team_db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute(
                "INSERT INTO bridge_messages ("
                "    team_id, recipient, seq, sender_id, "
                "    idempotency_key, causal_ref, kind, wave, "
                "    payload, persona_snapshot_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "T1",
                    "team-lead",
                    1,
                    "backend-engineer-1",
                    None,
                    None,
                    "reply",
                    None,
                    payload,
                    1,
                ),
            )
        msg = str(exc.value)
        # Either the CHECK clause name or the column name should appear.
        assert "CHECK" in msg or "payload" in msg, f"unexpected error: {msg!r}"
    finally:
        conn.close()


def test_ascii_payload_at_byte_cap(team_db: str) -> None:
    """Boundary: exactly 8192 ASCII bytes lands; 8193 raises."""
    at_cap = "a" * PAYLOAD_MAX_BYTES
    assert len(at_cap.encode("utf-8")) == PAYLOAD_MAX_BYTES
    r = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="backend-engineer-1",
        kind="reply",
        payload=at_cap,
    )
    assert r["seq"] == 1
    assert r["deduped"] is False

    over_cap = "b" * (PAYLOAD_MAX_BYTES + 1)
    with pytest.raises(PayloadTooLargeError):
        send(
            team_db,
            team_id="T1",
            recipient="team-lead",
            sender_id="backend-engineer-1",
            kind="reply",
            payload=over_cap,
        )


# ── Tests: H7 secret roundtrip with trailing newline ──────────────────────


def test_secret_with_trailing_newline_roundtrip(
    tmp_path: Path,
    team_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing newline in .secret survives HMAC roundtrip (no strip).

    Pre-H7 the writer called ``.strip()`` on the secret bytes, so a
    secret written as ``b"abc\\n"`` would HMAC-verify against tokens
    issued with ``b"abc"`` and silently mismatch any issuer that kept
    the newline. After H7 the writer reads the exact bytes and round-
    trips correctly.
    """
    role = "backend-engineer-1"
    tdir = tmp_path / ".atelier" / "team" / "T1"
    tdir.mkdir(parents=True)

    secret = b"abc\n"  # deliberate trailing newline
    (tdir / ".secret").write_bytes(secret)
    os.chmod(tdir / ".secret", 0o600)

    # Issue the token with the literal-bytes secret — same bytes the
    # writer will now read.
    tok = hmac.new(secret, role.encode(), sha256).hexdigest()
    (tdir / f"{role}.token").write_text(tok)
    os.chmod(tdir / f"{role}.token", 0o600)  # H8: 0o600

    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", role)
    argv = [
        "--team",
        "T1",
        "--to",
        "team-lead",
        "--kind",
        "reply",
        "--payload",
        "hello-secret-roundtrip",
        "--db",
        team_db,
        "--clone-root",
        str(tmp_path),
    ]
    rc = bridge_send.main(argv)
    assert rc == 0  # If H7 regressed (strip() re-introduced), rc would be 2.


# ── Tests: H8 permission enforcement on .secret and .token ────────────────


def test_token_file_world_readable_rejected(
    clone_root_with_token: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0o644 on the token file → SenderAuthError naming permissions."""
    tok_path = clone_root_with_token / ".atelier" / "team" / "T1" / "backend-engineer-1.token"
    os.chmod(tok_path, 0o644)
    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", "backend-engineer-1")
    with pytest.raises(SenderAuthError) as exc:
        bridge_send._resolve_sender(
            clone_root_with_token,
            "T1",
            allow_unsafe=False,
            unsafe_from=None,
        )
    msg = str(exc.value)
    assert "permissions" in msg or "0o644" in msg


def test_secret_file_group_readable_rejected(
    clone_root_with_token: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0o640 on the .secret file → SenderAuthError naming permissions."""
    secret_path = clone_root_with_token / ".atelier" / "team" / "T1" / ".secret"
    os.chmod(secret_path, 0o640)
    monkeypatch.setenv("ATELIER_TEAM_SELF_ROLE", "backend-engineer-1")
    with pytest.raises(SenderAuthError) as exc:
        bridge_send._resolve_sender(
            clone_root_with_token,
            "T1",
            allow_unsafe=False,
            unsafe_from=None,
        )
    msg = str(exc.value)
    assert "permissions" in msg or "0o640" in msg
