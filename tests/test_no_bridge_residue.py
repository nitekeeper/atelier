"""Residue guard for the M7 PR-B bridge dispatch-queue removal.

After M7 PR-B the bridge DISPATCH QUEUE is GONE:

  * ``QueueBridgeDispatchTools`` + the ``build_spawn_fn`` / ``build_poll_fn``
    factories + the ``BRIDGE_*`` tunables (``dispatch.py``),
  * the ``build_wave_dispatcher`` / ``build_wave_dispatcher_for_project``
    agent-team dispatcher factories (``atelier_entrypoint.py``),
  * the ``ATELIER_TRANSPORT=bridge`` option (now raises ``UnknownTransportError``),
  * the ``internal/bridge-poll`` per-turn servicer SKILL,

and so is the harness-team lifecycle it fed (``sweep_leaked_teams`` /
``team_teardown`` / ``abort.py``'s ``resolve_team_id`` + ``_enqueue_team_delete``
``bridge_requests`` coupling). The ``bridge_requests`` TABLE is dropped by
``migrations/shared/013_drop_bridge_requests.sql``.

This test fails LOUD if any of that dead surface creeps back, while ASSERTING the
inter-agent message WIRE (``bridge_messages`` / ``bridge_send`` / ``bridge_read``
/ ``bridge_payloads`` / ``team_meeting`` / ``status`` / ``dispatch.
_parse_reply_envelope``) is STILL present — the WIRE was deliberately KEPT (it
backs team_meeting/status/abort and is orthogonal to the deleted dispatch queue).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# The LIVE-SOURCE surface this guard polices: code + SKILLs + tests, where a dead
# queue symbol/recipe resurfacing means a real regression. Two homes are
# deliberately OUT of scope and NOT scanned:
#   * ``migrations/`` — immutable history; the ``bridge_requests`` CREATE (008),
#     rebuild (009), tasks-comment (010) and DROP (013) legitimately live there.
#   * narrative / historical docs — ``CHANGELOG.md`` (append-only release history)
#     and ``CLAUDE.md`` (operational charter whose rules cite originating
#     incidents) legitimately NAME removed mechanisms in past-tense/rationale.
#     Scanning them for deleted symbols yields false positives, not signal. Stale
#     TRACKED process-artifact docs are policed by ``.gitignore`` (the Memex-
#     canonical policy; the ``docs/scopes/`` gap that let one slip is closed in
#     this PR), not by a token grep.
_CODE_DIRS = ("scripts", "internal", "skills", "tests")
_EXTS = {".py", ".md", ".sql"}

# Exact tokens that must NOT reappear in the live source — the deleted queue +
# agent-team dispatcher + harness-team lifecycle surface. One token per distinct
# deleted symbol; ``build_wave_dispatcher`` also covers
# ``build_wave_dispatcher_for_project``.
_DELETED_SYMBOLS = (
    "QueueBridgeDispatchTools",
    "build_spawn_fn",
    "build_poll_fn",
    "build_wave_dispatcher",
    "TRANSPORT_BRIDGE",
    "BridgeDispatchError",
    "BRIDGE_PER_CALL_TIMEOUT_S",
    "sweep_leaked_teams",
    "team_teardown",
    "resolve_team_id",
    "_enqueue_team_delete",
)


def _scan_files():
    """Yield the live-source ``.py``/``.md``/``.sql`` files under ``_CODE_DIRS``
    (excluding this test itself). This is the surface a resurrected dead-queue
    symbol/recipe would land in; ``migrations/`` and narrative docs are out of
    scope by design (see the ``_CODE_DIRS`` comment)."""
    for d in _CODE_DIRS:
        for p in (REPO_ROOT / d).rglob("*"):
            if p.suffix in _EXTS and p.is_file() and p.resolve() != SELF:
                yield p


def test_no_deleted_queue_symbols_anywhere():
    """No deleted queue / dispatcher / lifecycle symbol survives in the tree."""
    offenders: dict[str, list[str]] = {}
    for p in _scan_files():
        text = p.read_text(encoding="utf-8")
        hits = [s for s in _DELETED_SYMBOLS if s in text]
        if hits:
            offenders[str(p.relative_to(REPO_ROOT))] = hits
    assert not offenders, f"deleted queue/lifecycle symbols resurfaced: {offenders}"


def test_bridge_requests_table_token_only_in_migrations():
    """The dropped TABLE token appears NOWHERE outside ``migrations/``.

    Its only legitimate home is the migration HISTORY (008/009/010/013) and
    narrative docs (out of scope); any hit in the live source
    (scripts/internal/skills/tests) means dead queue code crept back.
    """
    offenders = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in _scan_files()
        if "bridge_requests" in p.read_text(encoding="utf-8")
    )
    assert not offenders, (
        f"`bridge_requests` table token resurfaced outside migrations/: {offenders}"
    )


def test_bridge_poll_servicer_skill_deleted():
    """The per-turn queue servicer SKILL (and its dir) is gone."""
    assert not (REPO_ROOT / "internal" / "bridge-poll" / "SKILL.md").exists()
    assert not (REPO_ROOT / "internal" / "bridge-poll").exists()


def test_message_wire_preserved():
    """The KEPT inter-agent WIRE survives — orthogonal to the deleted queue."""
    for mod in (
        "bridge_send.py",
        "bridge_read.py",
        "bridge_payloads.py",
        "status.py",
        "team_meeting.py",
    ):
        assert (REPO_ROOT / "scripts" / mod).is_file(), f"WIRE module scripts/{mod} missing"
    # The wire TABLE is still referenced by the wire code.
    assert "bridge_messages" in (REPO_ROOT / "scripts" / "bridge_read.py").read_text(
        encoding="utf-8"
    )
    # The reply-envelope parser (imported by status.py) + the kept dispatch entry
    # point stayed in dispatch.py.
    from scripts import dispatch

    assert hasattr(dispatch, "_parse_reply_envelope")
    assert hasattr(dispatch, "dispatch_task")
    assert hasattr(dispatch, "is_host_transport")


def test_bridge_requests_table_dropped_after_full_migration():
    """DB-level proof (complements the grep): a fresh full migration run leaves no
    ``bridge_requests`` table and records migration 013."""
    from scripts.migrate import apply_migrations

    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "t.db")
        apply_migrations(db, REPO_ROOT / "migrations" / "shared")
        apply_migrations(db, REPO_ROOT / "migrations" / "local-only")
        con = sqlite3.connect(db)
        try:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            applied = {r[0] for r in con.execute("SELECT filename FROM migrations")}
        finally:
            con.close()
    assert "bridge_requests" not in tables, "bridge_requests table was not dropped by migration 013"
    assert "013_drop_bridge_requests.sql" in applied, "migration 013 not applied"
