"""Content-addressed out-of-band store for oversized bridge payloads.

This is AI-0-store of the cycle-1 "payload referencing" arc — the foundation
that bridge_send (substitute-on-write) and bridge_read (resolve-on-read)
both depend on. It backs ``migrations/shared/011_bridge_payload_refs.sql``.

Design contract (locked in Phase 3 mesh close):

* **Content-addressed.** A body is keyed by ``sha256(body.encode('utf-8'))``
  scoped to its ``team_id``. Identical bodies within a team collapse to one
  row, so the store write is naturally idempotent.

* **Idempotent write.** :func:`store` uses ``INSERT OR IGNORE``; storing the
  same body twice is a no-op that returns the same ref. This is what makes
  bridge_send's idempotency-replay safe — a replay racing a first-send
  dereferences an already-present row, never a missing pointer. Callers
  MUST write the body here BEFORE taking the bridge_send ``BEGIN IMMEDIATE``
  seq lock.

* **Byte-exact / multi-byte safe.** ``byte_len`` is computed once from the
  SAME ``body.encode('utf-8')`` pass that feeds the sha256, and the schema
  pins it with ``CHECK(byte_len = length(CAST(body AS BLOB)))`` — identical
  to 003's payload byte-count idiom. A non-ASCII body round-trips
  byte-for-byte through :func:`store` → :func:`get`.

* **No schema length cap.** The body deliberately has no 8 KiB CHECK (that is
  the cap this store exists to escape). A tunable application-layer ceiling
  (:data:`MAX_BODY_BYTES`, 1 MiB) guards against an unbounded blob sink
  without baking a limit into a migration.

* **Append-only.** The schema forbids UPDATE/DELETE on bridge_payloads, so
  a stored body can never mutate under its hash. Reclamation of orphaned
  rows is a DEFERRED team-teardown sweep (its own future migration); the
  ``team_id`` scope column exists so that path is never designed out.

No AUTOINCREMENT, no surrogate id — the content address IS the identity.
"""

from __future__ import annotations

import hashlib
import sqlite3

# Schema-version pin — matches scripts/bridge_send.py / bridge_read.py. The
# bridge wire protocol stays at 1; migration 011 is additive and does NOT
# bump user_version, so the store opens against the same pin as the writer
# and reader. A mismatch means the DB predates the team-mode bridge.
SCHEMA_VERSION = 1

# Application-layer sanity ceiling on a single out-of-band body. 1 MiB is far
# above any realistic inter-agent payload yet bounds a pathological/abusive
# body from filling the store. Tunable here WITHOUT a migration (deliberately
# not a schema CHECK). Counted in UTF-8 bytes, matching byte_len.
MAX_BODY_BYTES = 1024 * 1024


class SchemaVersionMismatch(RuntimeError):
    """Raised when the DB ``user_version`` does not match :data:`SCHEMA_VERSION`."""


class PayloadTooLarge(ValueError):
    """Raised when a body exceeds :data:`MAX_BODY_BYTES`."""


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite handle with the team-mode pragma bundle.

    Mirrors ``scripts/bridge_send._open_db`` (WAL + synchronous=NORMAL +
    busy_timeout + FK) so the store serializes cleanly against concurrent
    bridge writers on the same DB.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _verify_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA user_version").fetchone()
    found = int(row[0]) if row is not None else 0
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"DB schema_version mismatch: expected {SCHEMA_VERSION}, "
            f"found {found}. Apply migrations/shared/ (003 + 011) or pin "
            f"the caller to the on-disk version."
        )


def compute_sha256(body: str) -> str:
    """Return the hex sha256 of ``body`` over its UTF-8 encoding.

    The content address. Computed over the exact byte sequence stored as
    ``body`` and counted by ``byte_len`` — so hash, length and stored bytes
    can never disagree on a multi-byte body.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def store(conn: sqlite3.Connection, team_id: str, body: str) -> dict[str, object]:
    """Content-address ``body`` and persist it (idempotent).

    Computes the sha256 + UTF-8 byte length once, then ``INSERT OR IGNORE``
    into bridge_payloads. Storing a body already present is a no-op that
    returns the identical reference dict — the property bridge_send relies on
    for replay safety.

    Returns ``{"team_id", "sha256", "byte_len"}`` — the reference coordinates
    bridge_send substitutes into the message and bridge_read resolves.

    Raises :exc:`PayloadTooLarge` if the body exceeds :data:`MAX_BODY_BYTES`.
    """
    encoded = body.encode("utf-8")
    byte_len = len(encoded)
    if byte_len > MAX_BODY_BYTES:
        raise PayloadTooLarge(
            f"payload body is {byte_len} bytes; bridge_payloads caps a single "
            f"out-of-band body at {MAX_BODY_BYTES} bytes"
        )
    sha256 = hashlib.sha256(encoded).hexdigest()
    # INSERT OR IGNORE: the (team_id, sha256) PK makes a re-store of the same
    # body a silent no-op. We do NOT UPDATE on conflict — the append-only
    # triggers would reject it, and there is nothing to change (same content,
    # same hash, same length).
    conn.execute(
        "INSERT OR IGNORE INTO bridge_payloads (team_id, sha256, byte_len, body) "
        "VALUES (?, ?, ?, ?)",
        (team_id, sha256, byte_len, body),
    )
    return {"team_id": team_id, "sha256": sha256, "byte_len": byte_len}


def exists(conn: sqlite3.Connection, team_id: str, sha256: str) -> bool:
    """Return whether a body for ``(team_id, sha256)`` is present."""
    row = conn.execute(
        "SELECT 1 FROM bridge_payloads WHERE team_id = ? AND sha256 = ?",
        (team_id, sha256),
    ).fetchone()
    return row is not None


def get(conn: sqlite3.Connection, team_id: str, sha256: str) -> str | None:
    """Return the stored body for ``(team_id, sha256)``, or ``None``.

    Byte-exact: the returned ``str`` re-encodes to the exact UTF-8 bytes that
    were stored, so a multi-byte body round-trips unchanged. Resolution
    happens BEFORE any untrusted-data fencing — bridge_read re-wraps the
    returned body in its ``<untrusted ...>`` fence; this layer never does.
    """
    row = conn.execute(
        "SELECT body FROM bridge_payloads WHERE team_id = ? AND sha256 = ?",
        (team_id, sha256),
    ).fetchone()
    return None if row is None else str(row["body"])
