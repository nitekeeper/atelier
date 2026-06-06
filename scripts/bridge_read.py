# scripts/bridge_read.py
"""Cursor-based reader for the team-mode bridge log (epic #37).

Mesh-close contract (see docs/specs/2026-05-25-atelier-team-mode-design.md):

* Membership check: ``--as <role_id>`` MUST appear as a
  ``team_members(team_id, role_id)`` row. Refusing cross-channel snooping
  is enforced here (the bridge log itself carries the row but a SELECT
  against ``recipient=?`` would otherwise leak anyone's inbox to anyone
  who guesses the team_id). Auth-mismatch → exit 5.

* SCHEMA_VERSION runtime pin: identical mechanism to bridge_send.py — on
  open, assert ``PRAGMA user_version == SCHEMA_VERSION`` (=1). Mismatch
  → exit 7 with a message naming both versions.

* Default heartbeat exclusion: ``kind='heartbeat'`` rows are filtered
  OUT of the default pull. ``--include-heartbeats`` opts in. The mesh
  pushback was that heartbeats must not become a steganographic side
  channel — every consumer must explicitly ask to see them.

* UNTRUSTED fencing: every emitted JSONL row wraps the payload in
  ``<untrusted source="{sender_id}" seq="{seq}">{payload}</untrusted>``
  before it hits stdout. Bridge payloads are *data*, never instructions,
  at the consumer boundary; the fence is the syntactic signal that
  downstream prompts must treat the contents accordingly.

* bridge_delivery side-table: after returning rows, the reader UPSERTs
  ``bridge_delivery(team_id, recipient, last_seq, delivered_at)``.
  The bridge log itself is append-only (triggers reject UPDATE/DELETE),
  so the delivery cursor lives in its own mutable table per the Phase 3
  mesh close.

* ``--follow``: polls with 250 ms sleep on empty result, exponentially
  backing off to 2 s after ten empty ticks. Each poll opens a fresh
  read txn so WAL readers see new committed writes (snapshot isolation
  is per-transaction in SQLite WAL mode).

CLI surface (stable):

    bridge_read --team <team_id> --as <self_role_id>
                [--since-seq N] [--limit N=500]
                [--follow] [--timeout-ms M]
                [--include-heartbeats] [--resolve]
                [--db <path>]

* ``--resolve``: dereference out-of-band referenced payloads (rows whose
  first-class ``payload_ref`` column IS NOT NULL — the decision is keyed off
  the column, NEVER a regex over the fenced stub text, so a literal
  ``[bridge-ref ...]`` inside an inline untrusted body stays inert). The stored
  body is sha256-verified against ``payload_ref`` BEFORE emit and re-wrapped in
  the SAME ``<untrusted>`` fence — resolve preserves the trust boundary, it is
  not a laundering path. Fail-closed: a sha mismatch (tamper) or a missing row
  (dangling/GC'd ref) yields a FENCED error sentinel, never the raw body, plus
  a nonzero exit (9 / 8). Default OFF: the orchestrator carries reference
  stubs, not bodies (the F15 / 600 s context-budget win).

Stdout: JSON Lines, one object per message:

    {"seq": N, "sender_id": "...", "kind": "...",
     "payload": "<untrusted source=...>...</untrusted>",
     "causal_ref": M|null,
     "persona_snapshot_id": K,
     "created_at": "..."}

Exit codes (callers can branch on these without parsing stderr):

    0  ok (rows emitted, or follow-loop exited cleanly on --timeout-ms)
    2  argparse / generic CLI failure (argparse default)
    3  channel-missing (team_id not in `teams`)
    4  lock-timeout (SQLite busy_timeout exceeded under follow)
    5  auth-mismatch (--as is not a member of --team)
    7  schema-version-mismatch
    8  ref-not-found   (--resolve: a referenced body is missing / GC'd)
    9  ref-sha-mismatch (--resolve: a referenced body failed sha256 verify)
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
from typing import Any

from scripts import bridge_payloads

# Triple-pinned: see scripts/bridge_send.py SCHEMA_VERSION docstring.
SCHEMA_VERSION = 1

# Default pull batch size. data-engineer-1's covering index serves
# (team_id, recipient, seq) index-only at this fan-out without paging.
DEFAULT_LIMIT = 500

# Follow-loop tunables. Empty polls back off geometrically to spare the
# WAL pager; first ten polls at 250 ms catch most reply latencies, then
# we settle at 2 s for genuinely idle channels.
FOLLOW_INITIAL_MS = 250
FOLLOW_BACKOFF_AFTER = 10
FOLLOW_MAX_MS = 2000

# Exit codes — keep in lock-step with the docstring above.
EXIT_OK = 0
EXIT_CHANNEL_MISSING = 3
EXIT_LOCK_TIMEOUT = 4
EXIT_AUTH_MISMATCH = 5
EXIT_SCHEMA_VERSION = 7
# Resolve fail-closed codes (only reachable under --resolve). Distinct so a
# caller can tell a dangling/GC'd ref (8) from a tamper-detected sha mismatch
# (9). sha-mismatch outranks not-found when a batch hits both: tamper is the
# higher-severity signal.
EXIT_REF_NOT_FOUND = 8
EXIT_REF_SHA_MISMATCH = 9


# ── Exceptions ─────────────────────────────────────────────────────────────


class BridgeReadError(RuntimeError):
    """Base class for bridge_read failures with explicit exit codes."""

    exit_code: int = 1


class SchemaVersionMismatch(BridgeReadError):
    exit_code = EXIT_SCHEMA_VERSION


class ChannelMissingError(BridgeReadError):
    exit_code = EXIT_CHANNEL_MISSING


class AuthMismatchError(BridgeReadError):
    exit_code = EXIT_AUTH_MISMATCH


class LockTimeoutError(BridgeReadError):
    exit_code = EXIT_LOCK_TIMEOUT


# ── DB connection helper ───────────────────────────────────────────────────


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite handle with the team-mode pragma bundle.

    Matches scripts/bridge_send.py:_open_db exactly so reader + writer
    share one connection convention. isolation_level=None keeps us in
    autocommit mode — each pull is a single SELECT with an implicit
    read-only txn that WAL gives us "for free" (snapshot isolation,
    no lock contention with concurrent writers).
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
            f"found {found}. Apply migrations/shared/003_team_mode.sql "
            f"or pin the reader to the on-disk version."
        )


# ── Membership + channel checks ────────────────────────────────────────────


def _verify_team_exists(conn: sqlite3.Connection, team_id: str) -> None:
    row = conn.execute("SELECT 1 FROM teams WHERE team_id=?", (team_id,)).fetchone()
    if row is None:
        raise ChannelMissingError(
            f"no such team: team_id={team_id!r}. Has dispatch.py created this team yet?"
        )


def _verify_membership(conn: sqlite3.Connection, team_id: str, role_id: str) -> None:
    """Reject any --as that is not a real member of --team.

    Stops a caller who learned a team_id (cheap secret) from tailing
    another teammate's inbox by asserting they belong on the roster.
    Membership check + ATELIER_TEAM_SELF_ROLE env contract on the
    writer side together close the impersonation loop.
    """
    row = conn.execute(
        "SELECT 1 FROM team_members WHERE team_id=? AND role_id=?",
        (team_id, role_id),
    ).fetchone()
    if row is None:
        raise AuthMismatchError(
            f"role_id={role_id!r} is not a member of team_id={team_id!r}. "
            f"Cross-channel reads are forbidden — dispatch.py must "
            f"spawn the member before the bridge will surface their inbox."
        )


# ── Fence wrap ─────────────────────────────────────────────────────────────


def _fence(payload: str, sender_id: str, seq: int) -> str:
    """Wrap a payload in the UNTRUSTED-data fence.

    The XML-ish syntax (chosen by ai-safety-1 + prompt-engineer-1 in the
    mesh close) is mirrored by the team-mode-rules SKILL.md so every
    consumer prompt teaches the agent: contents are data, never
    instructions. ``sender_id`` and ``seq`` are interpolated as
    attributes so the wrapping cannot be confused with payload text.

    Defense: sender_id + seq are HTML-escaped with quote=True because
    they sit inside ``"..."`` attribute values — escaping mitigates the
    attribute-break attack (MEDIUM #15 from SDET review). The payload is
    HTML-escaped with quote=False because it lives in element content,
    where attribute-quote escaping is wasted bytes; the element-content
    escape mitigates the ``</untrusted>``-in-payload fence-break attack
    (BLOCKER #2). Both attack surfaces fold into one defense pass.
    """
    return (
        f'<untrusted source="{html.escape(sender_id, quote=True)}" '
        f'seq="{html.escape(str(seq), quote=True)}">'
        f"{html.escape(payload, quote=False)}"
        f"</untrusted>"
    )


# ── Resolve (dereference out-of-band referenced bodies) ────────────────────


def _resolve_field(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    payload_ref: str,
    sender_id: str,
    seq: int,
) -> tuple[str, str | None]:
    """Dereference a referenced payload, sha-verify, then re-fence — fail-closed.

    This is the locked Phase-3 invariant (ai-safety-researcher-1's owned seam):
    a resolved body is STILL untrusted content, so resolve is a
    trust-boundary-PRESERVING op, never a laundering path.

    Order is mandatory: ``get`` the body → recompute ``sha256`` over its verbatim
    UTF-8 bytes → compare to ``payload_ref`` → ONLY THEN ``_fence`` + emit.

    Fail-closed: on a missing row (dangling / GC'd ref) or a sha mismatch
    (tamper), return a FENCED error SENTINEL — NEVER the raw body — plus a
    distinct error tag (``"not-found"`` / ``"sha-mismatch"``). The sentinel
    stays inside the SAME ``<untrusted>`` fence so a failed resolve can never
    become an unfenced injection surface. Source/seq attribution is preserved
    on both the resolved body and the sentinel.

    Returns ``(fenced_payload, error_tag_or_None)``.
    """
    body = bridge_payloads.get(conn, team_id, payload_ref)
    if body is None:
        sentinel = f"[unresolved-ref: not-found sha256:{payload_ref}]"
        return _fence(sentinel, sender_id, seq), "not-found"
    actual = bridge_payloads.compute_sha256(body)
    if actual != payload_ref:
        sentinel = f"[unresolved-ref: sha-mismatch sha256:{payload_ref}]"
        return _fence(sentinel, sender_id, seq), "sha-mismatch"
    return _fence(body, sender_id, seq), None


# ── Pull / cursor update ───────────────────────────────────────────────────


def _pull(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    recipient: str,
    since_seq: int,
    limit: int,
    include_heartbeats: bool,
) -> list[sqlite3.Row]:
    """Single batch read. Returns ordered rows or [] on no-data.

    The query rides on ``ux_bridge_pkey`` (team_id, recipient, seq) so
    SQLite serves it index-only — important for the follow loop where
    we re-issue it every poll. The heartbeat filter is inlined into the
    SQL rather than post-filtered in Python so the WHERE clause does
    the right amount of work at the storage layer.
    """
    if include_heartbeats:
        sql = (
            "SELECT seq, sender_id, kind, payload, payload_ref, causal_ref, "
            "       persona_snapshot_id, created_at "
            "FROM bridge_messages "
            "WHERE team_id=? AND recipient=? AND seq > ? "
            "ORDER BY seq ASC LIMIT ?"
        )
        params: tuple[Any, ...] = (team_id, recipient, since_seq, limit)
    else:
        sql = (
            "SELECT seq, sender_id, kind, payload, payload_ref, causal_ref, "
            "       persona_snapshot_id, created_at "
            "FROM bridge_messages "
            "WHERE team_id=? AND recipient=? AND seq > ? "
            "  AND kind != 'heartbeat' "
            "ORDER BY seq ASC LIMIT ?"
        )
        params = (team_id, recipient, since_seq, limit)
    return list(conn.execute(sql, params).fetchall())


def _advance_cursor(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    recipient: str,
    last_seq: int,
) -> None:
    """Upsert (team_id, recipient) → last_seq into bridge_delivery.

    The mutable cursor lives in its OWN table because bridge_messages
    is append-only (triggers RAISE(ABORT) on UPDATE/DELETE). We
    never try to UPDATE the log row — that would tear the append-only
    guarantee on which idempotency replay rests. SQLite's ON CONFLICT
    upsert keeps the path single-roundtrip.
    """
    assert last_seq > 0, "cursor advance with last_seq=0 would overwrite a valid cursor"
    conn.execute(
        "INSERT INTO bridge_delivery (team_id, recipient, last_seq) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(team_id, recipient) DO UPDATE SET "
        "    last_seq=excluded.last_seq, "
        "    delivered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (team_id, recipient, last_seq),
    )


def _row_to_dict(
    row: sqlite3.Row,
    *,
    conn: sqlite3.Connection | None = None,
    team_id: str | None = None,
    resolve: bool = False,
) -> dict[str, Any]:
    """Render a bridge row as an emittable dict.

    A message is "referenced" iff ``payload_ref IS NOT NULL`` — the decision is
    keyed off the first-class COLUMN, never by regex-scanning the fenced payload
    text (D3 unforgeability: a literal ``[bridge-ref ...]`` stub sitting in an
    inline untrusted body has ``payload_ref = NULL`` and is therefore INERT).

    Default (``resolve=False``): the in-band stub already stored in ``payload``
    is fenced and emitted — the orchestrator carries the STUB, not the body
    (the F15 / 600 s context-savings win).

    ``resolve=True``: a referenced row is dereferenced + sha-verified + re-fenced
    via :func:`_resolve_field` (fail-closed). Inline rows are unaffected.
    """
    seq = int(row["seq"])
    sender_id = row["sender_id"]
    payload_ref = row["payload_ref"]
    out: dict[str, Any] = {
        "seq": seq,
        "sender_id": sender_id,
        "kind": row["kind"],
        "payload_ref": payload_ref,
        "causal_ref": int(row["causal_ref"]) if row["causal_ref"] is not None else None,
        "persona_snapshot_id": int(row["persona_snapshot_id"]),
        "created_at": row["created_at"],
    }
    if resolve and payload_ref is not None:
        assert conn is not None and team_id is not None, "resolve requires an open conn + team_id"
        fenced, err = _resolve_field(
            conn,
            team_id=team_id,
            payload_ref=str(payload_ref),
            sender_id=sender_id,
            seq=seq,
        )
        out["payload"] = fenced
        if err is not None:
            out["resolve_error"] = err
    else:
        out["payload"] = _fence(row["payload"], sender_id, seq)
    return out


# ── Public read entrypoint ─────────────────────────────────────────────────


def read_once(
    db_path: str,
    *,
    team_id: str,
    role_id: str,
    since_seq: int = 0,
    limit: int = DEFAULT_LIMIT,
    include_heartbeats: bool = False,
    update_cursor: bool = True,
    resolve: bool = False,
) -> list[dict[str, Any]]:
    """One-shot read. Returns fenced dicts; updates bridge_delivery.

    Importable surface used by tests + downstream tooling. The CLI
    shim emits these as JSONL.

    ``resolve``: when True, referenced rows (``payload_ref IS NOT NULL``) are
    dereferenced + sha-verified + re-fenced (fail-closed; see
    :func:`_resolve_field`). Default False keeps the F15 context-savings win —
    the orchestrator carries reference stubs, not bodies. Resolution reuses this
    one-shot's open connection and is a pure read: it never mutates the cursor.
    """
    conn = _open_db(db_path)
    try:
        _verify_schema_version(conn)
        _verify_team_exists(conn, team_id)
        _verify_membership(conn, team_id, role_id)
        rows = _pull(
            conn,
            team_id=team_id,
            recipient=role_id,
            since_seq=since_seq,
            limit=limit,
            include_heartbeats=include_heartbeats,
        )
        if rows and update_cursor:
            _advance_cursor(
                conn,
                team_id=team_id,
                recipient=role_id,
                last_seq=int(rows[-1]["seq"]),
            )
        # Eager list comp: any resolve dereference runs here, while `conn` is
        # still open (closed in the finally below).
        return [_row_to_dict(r, conn=conn, team_id=team_id, resolve=resolve) for r in rows]
    finally:
        conn.close()


# ── Follow loop ────────────────────────────────────────────────────────────


def _next_delay_ms(empty_polls: int) -> int:
    """Geometric backoff bounded by FOLLOW_MAX_MS.

    First FOLLOW_BACKOFF_AFTER polls stay at FOLLOW_INITIAL_MS; beyond
    that we double per poll until we hit FOLLOW_MAX_MS. Keeps idle
    channels cheap without sacrificing reply latency under load.
    """
    if empty_polls < FOLLOW_BACKOFF_AFTER:
        return FOLLOW_INITIAL_MS
    # Shift clamped at 8 → max multiplier 256. Once empty_polls >= FOLLOW_BACKOFF_AFTER + 8,
    # delay sits at FOLLOW_MAX_MS until an inbound row resets the counter.
    multiplier = 1 << min(empty_polls - FOLLOW_BACKOFF_AFTER, 8)
    return min(FOLLOW_INITIAL_MS * multiplier, FOLLOW_MAX_MS)


def _follow(
    db_path: str,
    *,
    team_id: str,
    role_id: str,
    since_seq: int,
    limit: int,
    include_heartbeats: bool,
    timeout_ms: int | None,
    out,
    resolve: bool = False,
    sleep=time.sleep,
    now=time.monotonic,
) -> int:
    """Loop, emitting JSONL rows as they arrive. Returns an exit code.

    ``sleep`` and ``now`` are dependency-injected so the test suite can
    drive the loop deterministically without burning real wall-clock.
    """
    cursor = since_seq
    empty_polls = 0
    start = now()
    # Track the worst fail-closed resolve_error across ALL polls so a bounded
    # --follow --resolve surfaces tamper/dangling signals at timeout, mirroring
    # the one-shot precedence (sha-mismatch outranks not-found). The fenced
    # sentinel already suppresses the body on each row; this only fixes the EXIT
    # CODE a caller branches on.
    worst_resolve_exit = EXIT_OK
    while True:
        # Open a fresh transaction per poll so WAL snapshot isolation lets us see
        # new committed writes; SQLite WAL snapshot is per-transaction, not per-connection.
        rows = read_once(
            db_path,
            team_id=team_id,
            role_id=role_id,
            since_seq=cursor,
            limit=limit,
            include_heartbeats=include_heartbeats,
            update_cursor=True,
            resolve=resolve,
        )
        if rows:
            for r in rows:
                out.write(json.dumps(r) + "\n")
                err = r.get("resolve_error")
                if err == "sha-mismatch":
                    worst_resolve_exit = EXIT_REF_SHA_MISMATCH
                elif err == "not-found" and worst_resolve_exit == EXIT_OK:
                    worst_resolve_exit = EXIT_REF_NOT_FOUND
            out.flush()
            cursor = max(r["seq"] for r in rows)
            empty_polls = 0
        else:
            empty_polls += 1

        if timeout_ms is not None:
            elapsed_ms = (now() - start) * 1000
            if elapsed_ms >= timeout_ms:
                return worst_resolve_exit

        sleep(_next_delay_ms(empty_polls) / 1000.0)


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge_read",
        description="Cursor-based reader for the team-mode bridge log.",
    )
    p.add_argument("--team", required=True, help="team_id")
    p.add_argument(
        "--as",
        required=True,
        dest="role_id",
        help="self role_id (must be a member of --team)",
    )
    p.add_argument(
        "--since-seq",
        type=int,
        default=0,
        dest="since_seq",
        help="resume cursor: return rows with seq > N (default: 0)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max rows per batch (default: {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--follow",
        action="store_true",
        help="tail new messages indefinitely (use --timeout-ms to bound)",
    )
    p.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        dest="timeout_ms",
        help="wall-clock budget for --follow before clean exit (default: none)",
    )
    p.add_argument(
        "--include-heartbeats",
        action="store_true",
        dest="include_heartbeats",
        help="opt in to kind='heartbeat' rows (default: filtered out)",
    )
    p.add_argument(
        "--resolve",
        action="store_true",
        help=(
            "dereference out-of-band referenced payloads (payload_ref set): "
            "sha-verify + re-fence the body; fail-closed on tamper/dangling "
            "(exit 9 / 8). Default OFF — the orchestrator carries reference "
            "stubs, not bodies (F15 context budget)."
        ),
    )
    p.add_argument(
        "--db",
        default=".ai/atelier.db",
        help="SQLite DB path (default: .ai/atelier.db)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.follow:
            return _follow(
                args.db,
                team_id=args.team,
                role_id=args.role_id,
                since_seq=args.since_seq,
                limit=args.limit,
                include_heartbeats=args.include_heartbeats,
                timeout_ms=args.timeout_ms,
                out=sys.stdout,
                resolve=args.resolve,
            )
        rows = read_once(
            args.db,
            team_id=args.team,
            role_id=args.role_id,
            since_seq=args.since_seq,
            limit=args.limit,
            include_heartbeats=args.include_heartbeats,
            resolve=args.resolve,
        )
        # Emit every row (including any fail-closed sentinel), THEN surface the
        # fail-closed exit code. sha-mismatch (tamper) outranks not-found.
        exit_code = EXIT_OK
        for r in rows:
            sys.stdout.write(json.dumps(r) + "\n")
            err = r.get("resolve_error")
            if err == "sha-mismatch":
                exit_code = EXIT_REF_SHA_MISMATCH
            elif err == "not-found" and exit_code == EXIT_OK:
                exit_code = EXIT_REF_NOT_FOUND
        return exit_code
    except BridgeReadError as e:
        print(f"bridge_read: {e}", file=sys.stderr)
        return e.exit_code
    except sqlite3.OperationalError as e:
        # busy_timeout exceeded under contention surfaces here. Prefer the
        # symbolic sqlite_errorname (Python 3.11+) over a string-match on the
        # error message — the symbol is part of SQLite's public C API and
        # stable across locales. Fall back to the legacy substring check on
        # older runtimes so we never silently mis-classify a BUSY as a
        # generic sqlite error (LOW item: wrong exit code in that branch).
        if getattr(e, "sqlite_errorname", None) == "SQLITE_BUSY":
            print(f"bridge_read: lock timeout: {e}", file=sys.stderr)
            return EXIT_LOCK_TIMEOUT
        # Documented fallback for Python < 3.11 where sqlite_errorname is absent.
        if "locked" in str(e).lower():
            print(f"bridge_read: lock timeout: {e}", file=sys.stderr)
            return EXIT_LOCK_TIMEOUT
        print(f"bridge_read: sqlite error: {e}", file=sys.stderr)
        return EXIT_CHANNEL_MISSING
    except sqlite3.Error as e:
        print(f"bridge_read: sqlite error: {e}", file=sys.stderr)
        return EXIT_CHANNEL_MISSING


if __name__ == "__main__":
    raise SystemExit(main())
