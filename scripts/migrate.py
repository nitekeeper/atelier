import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"

# "Object already exists"-class OperationalError substrings. When a
# not-yet-recorded migration's STATEMENT fails with one of these, the structural
# effect that statement would create is ALREADY present in the schema — the only
# sane cause is a ledger/schema desync (ledger behind, schema ahead). We treat
# that as ALREADY-APPLIED for THAT STATEMENT: skip it and continue to the next,
# mirroring the memex migration runner's per-statement reconcile guard. Every
# OTHER error propagates.
_ALREADY_EXISTS_MARKERS: tuple[str, ...] = (
    "duplicate column name",
    "already exists",  # covers "table X already exists", "index X already exists",
    # "trigger X already exists", "view X already exists" — SQLite phrases all
    # object-already-exists errors as "<kind> <name> already exists".
)


def _is_already_exists_error(exc: sqlite3.OperationalError) -> bool:
    """True iff `exc` is an 'object already exists'-class OperationalError.

    Narrowly scoped: matches only SQLite's duplicate-column / object-already-
    exists messages. A genuinely new failure (syntax error, missing table,
    constraint violation, disk I/O, etc.) does NOT match and propagates.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _ALREADY_EXISTS_MARKERS)


def _is_comment_or_blank(text: str) -> bool:
    """True iff `text` is only SQL line comments / blank lines (no statement)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into individual SQL statements.

    Uses the stdlib `sqlite3.complete_statement()` — the same tokenizer-aware
    boundary detector the sqlite3 shell uses — so string literals, comments, and
    trigger `BEGIN ... END` bodies (whose internal `;` do NOT end the statement)
    are handled correctly. We deliberately do NOT hand-roll a BEGIN/END/CASE
    counter: that approach mis-handles `CASE ... END`, bare `BEGIN;` / `COMMIT;`,
    and identifiers named `begin`/`end`. `complete_statement` sidesteps that
    whole class of bug.

    A statement boundary is a `;` at which the accumulated buffer forms one or
    more complete statements. Self-wrapped transaction control (`BEGIN;`,
    `COMMIT;` in migrations 005 / 014) emerges as its own individual statement,
    so the file's own transaction framing is preserved verbatim.
    """
    statements: list[str] = []
    buffer = ""
    for ch in sql:
        buffer += ch
        if ch == ";" and sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    # Trailing content after the last ';'. A pure-comment / whitespace tail is
    # discarded; anything else is handed through so a malformed (unterminated)
    # statement surfaces a real error rather than being silently dropped.
    tail = buffer.strip()
    if tail and not _is_comment_or_blank(tail):
        statements.append(tail)
    return statements


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + FK enforcement.

    Inlined into the migration runner because `scripts/db.py` was retired
    (Plan 3 Task 9). The migration runner is the only legitimate consumer
    of a raw SQLite handle that survives the Memex/Local backend split;
    business-logic callers go through `scripts/backend.py`.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_one_migration(conn: sqlite3.Connection, sql: str) -> None:
    """Apply a single migration file's statements PER-STATEMENT, with a
    per-statement already-exists reconcile.

    Why per-statement (not whole-file `executescript`): `executescript` aborts
    at the FIRST failing statement. If an already-exists statement precedes a
    genuinely-NEW statement in the same not-yet-recorded file (the real partial-
    `002` hazard: a prior run crashed after the first `ALTER ADD source_ref`,
    002 was never ledger-recorded, the guarded re-run hits `duplicate column
    name` on statement 1), a whole-file reconcile would record the file as
    applied while SILENTLY SKIPPING every later statement (the other two
    source_ref columns, the FTS table, the 3 triggers) — schema permanently
    incomplete while the ledger claims done. Per-statement reconcile fixes this:
    the collided statement is skipped, the genuinely-new statements after it
    still run.

    Transaction framing — the self-wrapped 005 / 014 interaction:
    The connection runs in AUTOCOMMIT mode (`isolation_level = None`, set by the
    caller), so Python opens NO implicit transaction. Each statement is then
    governed solely by the SQL file's OWN framing:
      * Self-wrapped rebuilds (005, 014) emit `BEGIN;` / `COMMIT;` as their own
        statements (split out by `_split_statements`); their `BEGIN` drives the
        transaction, so the whole DROP/RENAME rebuild is atomic. We deliberately
        do NOT open an outer SAVEPOINT/transaction — that would make the file's
        inner `BEGIN` raise "cannot start a transaction within a transaction",
        which `_is_already_exists_error` correctly does NOT swallow, so it would
        crash. Letting the file's BEGIN drive is the only correct choice.
      * Unwrapped additive files (002 source_ref, 007 metadata) autocommit each
        statement; a per-statement already-exists skip lets a partially-applied
        002 complete on re-run.

    On a mid-file NON-already-exists error inside a wrapped file, the caller's
    `rollback()` discards the still-open `BEGIN` block (nothing committed), so
    the store is never left half-applied.
    """
    for statement in _split_statements(sql):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if _is_already_exists_error(exc):
                # This statement's effect already exists (ledger/schema desync).
                # Skip ONLY this statement; later genuinely-new statements still
                # run. Every other OperationalError propagates to the caller.
                continue
            raise


def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    conn = get_connection(db_path)
    # Run in AUTOCOMMIT mode so each migration file's OWN transaction framing
    # drives — self-wrapped files (005 / 014) keep their `BEGIN;...COMMIT;`
    # atomicity; unwrapped additive files autocommit per statement. Python must
    # NOT open an implicit transaction that would collide with a file's `BEGIN`.
    conn.isolation_level = None
    try:
        # Naming convention: shared/ uses 001-049, local-only/ uses 050+.
        # The `migrations.filename` column is UNIQUE per file (not per dir) --
        # a collision between shared/050_foo.sql and local-only/050_foo.sql would
        # silently skip the second one. Enforce by convention.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)

        applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text()

            try:
                _apply_one_migration(conn, sql)
            except BaseException:
                # Roll back any still-open transaction (e.g. a wrapped file that
                # failed between its BEGIN and COMMIT) so we never leak an open
                # WAL transaction or leave the store half-applied + unrecorded.
                if conn.in_transaction:
                    conn.rollback()
                raise

            # Record the file ONLY after ALL its statements have been processed
            # (each either applied or skipped as already-present). In autocommit
            # mode this INSERT commits immediately.
            conn.execute(
                "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
                (migration_file.name, datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else ".ai/memex.db"
    # Local-mode default: apply shared/ then local-only/.
    # Memex-mode bootstrap supplies only shared/ via memex:core:create-store.
    shared = MIGRATIONS_DIR / "shared"
    local = MIGRATIONS_DIR / "local-only"
    apply_migrations(db_path, shared)
    apply_migrations(db_path, local)
    print(f"Migrations applied to {db_path}")
