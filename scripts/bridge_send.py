# scripts/bridge_send.py
"""Append-only writer for the team-mode bridge log (epic #37).

Mesh-close contract (see docs/specs/2026-05-25-atelier-team-mode-design.md):

* Sender authentication: the calling teammate's role_id comes from the
  environment variable ``ATELIER_TEAM_SELF_ROLE`` (set by dispatch.py in
  the spawn environment). The token file
  ``<clone>/.atelier/team/<team_id>/<role_id>.token`` (0600) carries an
  HMAC-SHA256 of the role_id under the team secret
  ``<clone>/.atelier/team/<team_id>/.secret``. The writer recomputes the
  HMAC and constant-time-compares before accepting the sender. The
  ``--allow-unsafe-sender`` flag bypasses both checks and is intended
  for tests only.

* Sequence allocation: ``BEGIN IMMEDIATE`` → ``SELECT
  COALESCE(MAX(seq), 0) + 1 FROM bridge_messages WHERE team_id=? AND
  recipient=?`` → ``INSERT`` → ``COMMIT``. The composite uniqueness on
  (team_id, recipient, seq) plus the IMMEDIATE lock keep the per-recipient
  FIFO invariant gap-free under concurrent writers.

* Idempotency: a per-team UNIQUE partial index on
  ``(team_id, idempotency_key)`` enforces "one send call, one row".
  Replay returns the *original* seq and persona_snapshot_id — never
  re-stamps under a newer persona.

* SCHEMA_VERSION pin: on DB open, the writer asserts
  ``PRAGMA user_version == SCHEMA_VERSION`` (=1, matching the
  003_team_mode.sql ``PRAGMA user_version = 1``). Mismatch → hard fail
  with an explicit message so a stale migration can never silently
  scribble into the log.

* Connection pragmas: ``journal_mode=WAL``, ``synchronous=NORMAL``,
  ``busy_timeout=5000``, ``foreign_keys=ON`` — applied on every open,
  mirroring ``scripts/migrate.py:get_connection`` and
  ``scripts/backend_local._conn``.

* Payload ceiling: 8 KiB hard cap (matches the CHECK on
  ``bridge_messages.payload``). Rejected at the writer before the
  ``BEGIN IMMEDIATE`` opens so we never hold the write lock to fail.

CLI surface (stable):

    bridge_send --team <team_id> --to <recipient_role_id>
                [--kind spawn|reply|shutdown_req|shutdown_resp|heartbeat]
                --payload <text|@path>
                [--idem <ulid>]
                [--causal-ref <seq>]
                [--allow-unsafe-sender [--from <role_id>]]
                [--db <path>]
                [--clone-root <path>]

Stdout: a single JSON object — ``{"seq": N, "deduped": bool,
"persona_snapshot_id": M}`` — followed by a trailing newline. Matches
the JSON-stdout convention of ``scripts/agents.py`` and friends.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import sqlite3
import stat
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

# Triple-pinned against:
#   * migrations/shared/003_team_mode.sql tail (PRAGMA user_version = 1)
#   * scripts/bridge_read.py  (TODO: same constant once implemented)
#   * internal/team-mode-rules/SKILL.md
# Bump requires a CHANGELOG entry and a new migration. Do not edit in place.
SCHEMA_VERSION = 1

# Hard cap on bridge payload bytes. Mirrors the CHECK on
# bridge_messages.payload so the CLI fails fast (no held write lock).
# Pre-escape contract: cap is measured on the RAW UTF-8 bytes as the sender
# wrote them. bridge_read._fence later HTML-escapes for display (< → &lt;,
# etc.), so the rendered fence can exceed PAYLOAD_MAX_BYTES; that rendered
# string is never stored. Do NOT "fix" this by measuring the escaped form.
PAYLOAD_MAX_BYTES = 8192

# 26-char Crockford-base32 ULID. The schema does not CHECK the length
# (per Phase-3 mesh-close we kept the CHECK off the column to keep the
# door open for UUID7 later), but the writer rejects non-conforming
# tokens so the audit trail stays consistent.
ULID_LEN = 26

# Allowed kinds — mirrors the CHECK on bridge_messages.kind. Re-validated
# at the CLI layer so we fail with a clear error instead of a generic
# SQLite IntegrityError.
ALLOWED_KINDS = frozenset({"spawn", "reply", "shutdown_req", "shutdown_resp", "heartbeat"})


# ── Exceptions ─────────────────────────────────────────────────────────────


class BridgeSendError(RuntimeError):
    """Base class for explicit bridge_send failures.

    Subclasses carry actionable messages so the operator (or the calling
    agent) gets a one-line diagnostic instead of a SQLite stack trace.
    """


class SchemaVersionMismatch(BridgeSendError):
    pass


class SenderAuthError(BridgeSendError):
    pass


class PayloadTooLargeError(BridgeSendError):
    pass


# ── DB connection helper ───────────────────────────────────────────────────


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite handle with the team-mode pragma bundle.

    Mirrors ``scripts/backend_local._conn`` (WAL + synchronous=NORMAL +
    FK) and additionally sets ``busy_timeout=5000`` per the Phase-3
    mesh-close contract — concurrent writers serialize on
    ``BEGIN IMMEDIATE`` and need a generous busy timeout so the
    test_bridge_concurrency 32-writer matrix never spuriously raises
    ``database is locked``.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    # We use isolation_level=None (autocommit) so explicit
    # `BEGIN IMMEDIATE` statements work; SQLite's default DEFERRED
    # behavior would otherwise wrap our explicit BEGIN in an outer
    # transaction. In autocommit mode the explicit BEGIN IMMEDIATE
    # below is the *only* lock acquisition — sqlite3's implicit
    # transaction would otherwise have already opened a DEFERRED txn
    # on the first SELECT and we'd race the seq allocator. See L18.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _verify_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA user_version").fetchone()
    # PRAGMA returns a single integer in column 0.
    found = int(row[0]) if row is not None else 0
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"DB schema_version mismatch: expected {SCHEMA_VERSION}, "
            f"found {found}. Apply migrations/shared/003_team_mode.sql "
            f"or pin the writer to the on-disk version."
        )


# ── Sender authentication ──────────────────────────────────────────────────


def _team_dir(clone_root: Path, team_id: str) -> Path:
    """Resolve the per-team secrets directory.

    Layout:
        <clone_root>/.atelier/team/<team_id>/.secret      0600
        <clone_root>/.atelier/team/<team_id>/<role>.token 0600

    The secrets directory is written by ``scripts/dispatch.py`` at
    team-creation / member-spawn time; the writer never creates files
    here — missing files = SenderAuthError.
    """
    return clone_root / ".atelier" / "team" / team_id


def _expected_token(secret: bytes, role_id: str) -> str:
    """HMAC-SHA256(secret, role_id) → hex digest.

    Mirrors the token issuance in dispatch.py. Kept as a pure function
    so the test suite can synthesize valid tokens without spinning up
    dispatch.
    """
    return hmac.new(secret, role_id.encode("utf-8"), sha256).hexdigest()


def _require_owner_only_perms(path: Path) -> None:
    """Raise SenderAuthError if ``path`` is group- or world-accessible.

    The team .secret and the per-role .token files are HMAC auth
    material — any non-owner readability lets a co-tenant process forge
    sender identity. Mode bits beyond 0o700 (i.e. anything in 0o077) are
    rejected with the actual octal mode in the error message so the
    operator can `chmod 600` and retry without guessing.
    """
    mode = os.stat(path).st_mode
    if mode & 0o077 != 0:
        raise SenderAuthError(
            f"insecure permissions on {path}: {oct(stat.S_IMODE(mode))} "
            f"(must be 0o600 — group/world bits forbidden on bridge "
            f"auth material)."
        )


def _verify_sender_token(clone_root: Path, team_id: str, role_id: str) -> None:
    """Verify the calling process owns the sender role_id.

    Reads ``<team_dir>/.secret`` + ``<team_dir>/<role_id>.token``,
    recomputes the HMAC, and compares with ``hmac.compare_digest`` so
    timing leaks are off the table. Any missing file or mismatched
    digest raises ``SenderAuthError`` with a specific reason — the
    operator/agent should never have to guess which file is wrong.
    """
    tdir = _team_dir(clone_root, team_id)
    secret_path = tdir / ".secret"
    token_path = tdir / f"{role_id}.token"

    if not secret_path.is_file():
        raise SenderAuthError(
            f"team secret missing: {secret_path}. dispatch.py must seed "
            f"the team before bridge_send may write."
        )
    if not token_path.is_file():
        raise SenderAuthError(
            f"sender token missing: {token_path}. Either dispatch.py has "
            f"not yet spawned role_id={role_id!r} into team_id={team_id!r}, "
            f"or ATELIER_TEAM_SELF_ROLE is set to the wrong role."
        )

    # H8: enforce 0o600 (owner-only) on both the team secret AND the
    # per-role token file. Any group/world permission bit means the
    # auth material is reachable by other processes on the box, and the
    # writer refuses to trust it. Two separate stat() calls so the
    # operator gets a precise diagnostic naming the offending path.
    _require_owner_only_perms(secret_path)
    _require_owner_only_perms(token_path)

    # Secret is the exact bytes of the .secret file. Do NOT strip() — a writer
    # that appends a trailing newline (e.g. `echo > .secret`) would silently
    # change the HMAC material and break auth round-trips.
    secret = secret_path.read_bytes()
    presented = token_path.read_text().strip()
    expected = _expected_token(secret, role_id)
    if not hmac.compare_digest(presented, expected):
        raise SenderAuthError(
            f"sender token HMAC mismatch for role_id={role_id!r}. The "
            f"token file may have been tampered with or the team secret "
            f"rotated without re-issuing tokens."
        )


def _resolve_sender(
    clone_root: Path,
    team_id: str,
    *,
    allow_unsafe: bool,
    unsafe_from: str | None,
) -> str:
    """Return the verified sender role_id for this invocation.

    Normal path: reads ``ATELIER_TEAM_SELF_ROLE`` from the environment
    (set by dispatch.py in the teammate's spawn env), then verifies the
    HMAC token. Unsafe path: ``--allow-unsafe-sender`` + ``--from
    <role>`` bypasses both env and token — strictly for unit tests.
    """
    if allow_unsafe:
        if not unsafe_from:
            raise SenderAuthError(
                "--allow-unsafe-sender requires --from <role_id>. "
                "Refusing to invent a sender identity."
            )
        return unsafe_from

    role_id = os.environ.get("ATELIER_TEAM_SELF_ROLE")
    if not role_id:
        raise SenderAuthError(
            "ATELIER_TEAM_SELF_ROLE is unset. bridge_send must be invoked "
            "from a teammate process spawned by dispatch.py (which sets "
            "this env), or pass --allow-unsafe-sender --from <role_id> "
            "for tests."
        )
    _verify_sender_token(clone_root, team_id, role_id)
    return role_id


# ── Payload handling ───────────────────────────────────────────────────────


def _load_payload(spec: str) -> str:
    """Resolve ``--payload`` argument. ``@<path>`` reads a file, else
    treats the value as a literal string.

    Length is checked before any DB lock acquisition so an oversize
    payload never blocks other writers.
    """
    if spec.startswith("@"):
        path = Path(spec[1:])
        data = path.read_text()
    else:
        data = spec
    if len(data.encode("utf-8")) > PAYLOAD_MAX_BYTES:
        raise PayloadTooLargeError(
            f"payload is {len(data.encode('utf-8'))} bytes; the bridge log "
            f"caps individual messages at {PAYLOAD_MAX_BYTES} bytes "
            f"(prompt-engineer-1's writer ceiling, enforced by CHECK on "
            f"bridge_messages.payload)."
        )
    return data


def _validate_idem(idem: str | None) -> None:
    if idem is None:
        return
    if len(idem) != ULID_LEN:
        raise BridgeSendError(
            f"--idem must be a {ULID_LEN}-char Crockford-base32 ULID "
            f"(got {len(idem)} chars). Generate via `ulid-py` or any "
            f"compatible library."
        )


# ── Core writer ────────────────────────────────────────────────────────────


def _lookup_persona_snapshot(conn: sqlite3.Connection, team_id: str, sender_id: str) -> int:
    """Fetch the persona_snapshot_id pinned to (team_id, sender_id).

    The bridge log denormalizes this column so a later persona-snapshot
    edit (forbidden by trigger but defended in depth here too) cannot
    rewrite the persona under which a historical message was sent.
    Missing row = the sender has never been dispatched into this team,
    which the composite sender FK would catch on INSERT anyway — we
    surface it earlier so the operator sees a clear "no such member"
    instead of an opaque foreign-key failure.
    """
    row = conn.execute(
        "SELECT persona_snapshot_id FROM team_members WHERE team_id=? AND role_id=?",
        (team_id, sender_id),
    ).fetchone()
    if row is None:
        raise SenderAuthError(
            f"no team_members row for team_id={team_id!r}, role_id="
            f"{sender_id!r}. dispatch.py must spawn the member before "
            f"the bridge will accept their writes."
        )
    return int(row["persona_snapshot_id"])


def send(
    db_path: str,
    *,
    team_id: str,
    recipient: str,
    sender_id: str,
    kind: str,
    payload: str,
    idempotency_key: str | None = None,
    causal_ref: int | None = None,
    wave: int | None = None,
) -> dict[str, Any]:
    """Append a message to the bridge log; return seq + dedupe status.

    Importable surface used by tests + dispatch.py. The CLI shim
    (``main``) is a thin argparse/JSON wrapper around this function.
    """
    if kind not in ALLOWED_KINDS:
        raise BridgeSendError(f"--kind must be one of {sorted(ALLOWED_KINDS)}; got {kind!r}.")
    _validate_idem(idempotency_key)

    # H5: enforce the byte cap on every code path that reaches send(),
    # not just the CLI's _load_payload(). dispatch.py (and any other
    # importer) passes a pre-loaded payload directly to send() and must
    # see the same PayloadTooLargeError the CLI surfaces — otherwise an
    # oversize payload would only fail at the schema CHECK, after we've
    # already paid the cost of opening the DB and (worse) taken the
    # BEGIN IMMEDIATE lock.
    payload_bytes = len(payload.encode("utf-8"))
    if payload_bytes > PAYLOAD_MAX_BYTES:
        raise PayloadTooLargeError(
            f"payload is {payload_bytes} bytes; the bridge log caps "
            f"individual messages at {PAYLOAD_MAX_BYTES} bytes (mirrors "
            f"the CHECK on bridge_messages.payload)."
        )

    conn = _open_db(db_path)
    try:
        _verify_schema_version(conn)
        persona_snapshot_id = _lookup_persona_snapshot(conn, team_id, sender_id)

        # Idempotency fast-path: if a row already exists for this team +
        # idempotency_key, return its seq + persona_snapshot_id without
        # taking a write lock. The (team_id, idempotency_key) UNIQUE
        # partial index makes this a single index seek.
        #
        # L18: this dedupe fast-path SELECT runs in autocommit, outside
        # any explicit BEGIN. A concurrent writer that COMMITs the same
        # idempotency_key between this SELECT and our INSERT is caught
        # by the IntegrityError recovery path (H4 fix). The fast-path
        # is an optimization, not the correctness boundary.
        if idempotency_key is not None:
            prior = conn.execute(
                "SELECT seq, persona_snapshot_id FROM bridge_messages "
                "WHERE team_id=? AND idempotency_key=?",
                (team_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                return {
                    "seq": int(prior["seq"]),
                    "deduped": True,
                    "persona_snapshot_id": int(prior["persona_snapshot_id"]),
                }

        # Allocator. BEGIN IMMEDIATE acquires the RESERVED lock up front
        # so the MAX(seq)+1 read and the INSERT cannot interleave with
        # another writer — the per-(team, recipient) seq stream stays
        # gap-free under fan-in.
        conn.execute("BEGIN IMMEDIATE")
        try:
            next_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM bridge_messages WHERE team_id=? AND recipient=?",
                (team_id, recipient),
            ).fetchone()
            next_seq = int(next_seq_row["next_seq"])
            try:
                conn.execute(
                    "INSERT INTO bridge_messages ("
                    "    team_id, recipient, seq, sender_id, "
                    "    idempotency_key, causal_ref, kind, wave, "
                    "    payload, persona_snapshot_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        recipient,
                        next_seq,
                        sender_id,
                        idempotency_key,
                        causal_ref,
                        kind,
                        wave,
                        payload,
                        persona_snapshot_id,
                    ),
                )
            except sqlite3.IntegrityError as e:
                # Idempotency race: a concurrent writer landed the same
                # key between our fast-path lookup and our INSERT.
                # Resolve by re-reading the prior row inside the IMMEDIATE
                # txn (so we know it's durable), then return the prior
                # seq. Any other IntegrityError (CHECK failure, FK
                # violation) re-raises.
                #
                # H4: SQLite's actual UNIQUE-violation message names the
                # offending columns, e.g.
                #   "UNIQUE constraint failed: bridge_messages.team_id,
                #    bridge_messages.idempotency_key"
                # The index *name* (ux_bridge_idem) does NOT appear in
                # the runtime message, so any match on the index name is
                # dead code. We match on the column SQLite actually
                # surfaces (and require team_id to be mentioned too, so
                # an unrelated future UNIQUE involving idempotency_key
                # can't be mis-classified).
                #
                # N22: this inner branch RETURNS normally on dedupe
                # replay, so the outer `except Exception` won't fire —
                # the inner branch must release the IMMEDIATE lock
                # itself. If the inner branch re-raises (prior row
                # vanished, or non-idempotency UNIQUE violation), the
                # outer except cleans up via
                # contextlib.suppress(OperationalError), which is the
                # intentional swallow of "no transaction" if SQLite has
                # already auto-rolled-back on the IntegrityError.
                msg = str(e)
                if idempotency_key is not None and "idempotency_key" in msg and "team_id" in msg:
                    prior = conn.execute(
                        "SELECT seq, persona_snapshot_id FROM bridge_messages "
                        "WHERE team_id=? AND idempotency_key=?",
                        (team_id, idempotency_key),
                    ).fetchone()
                    if prior is None:
                        # Should not happen — UNIQUE fired but row vanished.
                        # Re-raise so the outer cleanup path runs.
                        raise
                    # Release the IMMEDIATE lock before returning; the
                    # outer except handler will not fire on a normal
                    # return from this branch.
                    conn.execute("ROLLBACK")
                    return {
                        "seq": int(prior["seq"]),
                        "deduped": True,
                        "persona_snapshot_id": int(prior["persona_snapshot_id"]),
                    }
                raise
            conn.execute("COMMIT")
        except Exception:
            # Best-effort rollback. SQLite tolerates ROLLBACK on an
            # already-rolled-back txn (raises but we swallow); leaving
            # the IMMEDIATE txn open would otherwise wedge the next
            # writer until busy_timeout fires.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise

        return {
            "seq": next_seq,
            "deduped": False,
            "persona_snapshot_id": persona_snapshot_id,
        }
    finally:
        conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge_send",
        description="Append a message to the team-mode bridge log.",
    )
    p.add_argument("--team", required=True, help="team_id")
    p.add_argument("--to", required=True, dest="recipient", help="recipient role_id")
    p.add_argument(
        "--kind",
        default="reply",
        choices=sorted(ALLOWED_KINDS),
        help="message kind (default: reply)",
    )
    p.add_argument(
        "--payload",
        required=True,
        help="literal payload text, or @<path> to read from a file",
    )
    p.add_argument("--idem", default=None, help="26-char ULID idempotency key")
    p.add_argument(
        "--causal-ref",
        type=int,
        default=None,
        dest="causal_ref",
        help="seq this message replies to (cog-sci-1 adjacency anchor)",
    )
    p.add_argument(
        "--wave",
        type=int,
        default=None,
        help="dispatch wave this message belongs to (optional)",
    )
    p.add_argument(
        "--allow-unsafe-sender",
        action="store_true",
        dest="allow_unsafe_sender",
        help="bypass HMAC token verification (tests only)",
    )
    p.add_argument(
        "--from",
        default=None,
        dest="unsafe_from",
        help="sender role_id; requires --allow-unsafe-sender",
    )
    p.add_argument(
        "--db",
        default=".ai/atelier.db",
        help="SQLite DB path (default: .ai/atelier.db)",
    )
    p.add_argument(
        "--clone-root",
        default=".",
        dest="clone_root",
        help="repo root holding .atelier/team/<id>/ (default: CWD)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        payload = _load_payload(args.payload)
        sender_id = _resolve_sender(
            Path(args.clone_root).resolve(),
            args.team,
            allow_unsafe=args.allow_unsafe_sender,
            unsafe_from=args.unsafe_from,
        )
        result = send(
            args.db,
            team_id=args.team,
            recipient=args.recipient,
            sender_id=sender_id,
            kind=args.kind,
            payload=payload,
            idempotency_key=args.idem,
            causal_ref=args.causal_ref,
            wave=args.wave,
        )
    except BridgeSendError as e:
        # Surface explicit, actionable errors on stderr; reserve stdout
        # for the JSON success object so callers can pipe it.
        print(f"bridge_send: {e}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        print(f"bridge_send: sqlite error: {e}", file=sys.stderr)
        return 3

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
