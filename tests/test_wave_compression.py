"""Tests for wave-summary context compression in ``scripts/pm_dispatch.py``.

The feature retains validated reply envelope bodies per wave and, at the wave
boundary, replaces the verbatim bulk with a deterministic digest WHEN the
accumulated reply bytes cross a (env-tunable) threshold. Properties under test:

* threshold-NOT-crossed → no compression; the wave summary is byte-identical to
  pre-feature (no ``compressed`` / ``digest`` keys).
* threshold-crossed → ``summarize_fn`` is invoked ONCE per wave over the retained
  envelopes; the digest lands in the wave summary; the verbatim bulk is gone.
* metadata preserved through the digest: status / task_id / attempt survive, and
  an ``abandoned`` envelope's line-1 (which must still match ABANDON_RE) survives
  VERBATIM.
* :func:`default_wave_digest` is deterministic + pure + materially smaller than
  the input bytes (a measured byte reduction, not a vibe).
* a garbage ``ATELIER_COMPRESS_THRESHOLD`` env value is ignored (default used).
* ``summarize_fn=None`` falls back to :func:`default_wave_digest`.

All seams are pure stubs — no real sleeps, no real LLM, no unbounded loop, an
advanceable FakeClock only (mirrors tests/test_pm_dispatch.py's style). The
``summarize_fn`` stub is a pure ``lambda envs: "DIGEST"`` — NEVER a spawn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.migrate import apply_migrations
from scripts.pm_dispatch import (
    ABANDON_RE,
    COMPRESSION_THRESHOLD_BYTES,
    WaveDispatcher,
    default_wave_digest,
)
from scripts.pm_dispatch_envelope import validate_envelope

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── Local-mode DB fixture (mirrors tests/test_pm_dispatch.py) ────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    now = "2026-05-29T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("proj", "repo:proj", "Proj", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "p", "P", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db), "project_id": proj_id}


def _seed_task(workspace, *, title, parallel_group, created_at="2026-05-29T00:00:00Z"):
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "parallel_group, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace["project_id"],
            title,
            "d",
            "pending",
            parallel_group,
            "atelier-pm-1",
            created_at,
            created_at,
        ),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _task_row(workspace, task_id):
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row)


# ── Seams ────────────────────────────────────────────────────────────────────


class FakeClock:
    """Manually-advanceable monotonic clock — the ONLY way time moves here."""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def _done_envelope(task_id, attempt, *, notes_md="done", artifacts=None):
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": "done",
        "artifacts": [{"path": "f.py", "sha": "s"}] if artifacts is None else artifacts,
        "notes_md": notes_md,
        "next_action": "review",
    }


def _abandon_envelope(task_id, attempt, *, line1="ABANDON: scope:out of scope", tail=""):
    notes = line1 if not tail else f"{line1}\n{tail}"
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": "abandoned",
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": notes,
        "next_action": "none",
    }


# A bulk body big enough to cross the threshold on its own.
_BIG_NOTES = "X" * (COMPRESSION_THRESHOLD_BYTES + 4096)


# ── threshold NOT crossed → byte-identical to pre-feature ────────────────────


def test_small_wave_not_compressed_summary_identical(workspace):
    """A small wave (well under the threshold) is NOT compressed: the summary
    carries none of the compression keys, byte-identical to pre-feature."""
    tid = _seed_task(workspace, title="small", parallel_group=0)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt),
        clock=FakeClock(),
        summarize_fn=lambda envs: "DIGEST",  # pure stub; must NOT be called
    )
    summaries = d.run([_task_row(workspace, tid)])

    assert len(summaries) == 1
    s = summaries[0]
    assert "compressed" not in s
    assert "digest" not in s
    assert "reply_bytes" not in s
    # The pre-feature summary keys are exactly the WaveTracker.summary set.
    assert set(s) == {
        "wave_id",
        "expected",
        "reports",
        "outstanding",
        "complete",
        "terminal_only",
    }


def test_summarize_fn_not_invoked_on_small_wave(workspace):
    """summarize_fn is NEVER called when the threshold is not crossed."""
    tid = _seed_task(workspace, title="small", parallel_group=0)
    calls = []

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt),
        clock=FakeClock(),
        summarize_fn=lambda envs: calls.append(envs) or "DIGEST",
    )
    d.run([_task_row(workspace, tid)])
    assert calls == []


# ── threshold crossed → compressed once per wave ─────────────────────────────


def test_large_wave_compressed_summarize_invoked_once(workspace):
    """A wave whose accumulated reply bytes exceed the threshold is compressed:
    summarize_fn is invoked EXACTLY once for the wave, over the retained
    envelopes, and the digest lands in the summary while the verbatim bulk is
    gone."""
    tid = _seed_task(workspace, title="big", parallel_group=0)
    calls = []

    def summarize_fn(envs):
        calls.append(list(envs))
        return "DIGEST-RESULT"

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt, notes_md=_BIG_NOTES),
        clock=FakeClock(),
        summarize_fn=summarize_fn,
    )
    summaries = d.run([_task_row(workspace, tid)])

    assert len(calls) == 1  # once per wave
    assert len(calls[0]) == 1  # the one retained envelope
    s = summaries[0]
    assert s["compressed"] is True
    assert s["digest"] == "DIGEST-RESULT"
    assert s["reply_bytes"] > COMPRESSION_THRESHOLD_BYTES
    # The verbatim bulk is NOT carried verbatim in the summary.
    assert _BIG_NOTES not in repr(s)


def test_compression_is_per_wave_resets_between_waves(workspace):
    """Two waves, only the FIRST big: compression fires for wave-0 only; the
    accumulator resets so the small wave-1 is untouched."""
    t0 = _seed_task(workspace, title="w0-big", parallel_group=0)
    t1 = _seed_task(workspace, title="w1-small", parallel_group=1)
    calls = []

    def poll_fn(task, attempt):
        if task["id"] == t0:
            return _done_envelope(task["id"], attempt, notes_md=_BIG_NOTES)
        return _done_envelope(task["id"], attempt)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=poll_fn,
        clock=FakeClock(),
        summarize_fn=lambda envs: calls.append(envs) or "D",
    )
    summaries = d.run([_task_row(workspace, t0), _task_row(workspace, t1)])

    assert len(calls) == 1  # only wave-0
    assert summaries[0].get("compressed") is True
    assert "compressed" not in summaries[1]


# ── metadata preserved through the digest ────────────────────────────────────


def test_metadata_preserved_through_digest(workspace):
    """status / task_id / attempt survive verbatim in the default digest, even
    when the bulk is compressed."""
    tid = _seed_task(workspace, title="meta", parallel_group=0)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt, notes_md=_BIG_NOTES),
        clock=FakeClock(),
        # summarize_fn=None → default deterministic digest
    )
    summaries = d.run([_task_row(workspace, tid)])
    digest = summaries[0]["digest"]
    assert f"task_id={tid!r}" in digest
    assert "attempt=1" in digest
    assert "status='done'" in digest


def test_abandoned_line1_survives_verbatim_and_matches_abandon_re(workspace):
    """An abandoned envelope's line-1 survives the digest VERBATIM and still
    matches ABANDON_RE (it is re-parsed by _parse_abandon_category downstream).
    The bulk BELOW line-1 is what gets compressed."""
    tid = _seed_task(workspace, title="aband", parallel_group=0)
    line1 = "ABANDON: scope:out of scope"
    big_tail = "Y" * (COMPRESSION_THRESHOLD_BYTES + 2048)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _abandon_envelope(
            task["id"], attempt, line1=line1, tail=big_tail
        ),
        clock=FakeClock(),
        sleep_fn=lambda s: None,
    )
    summaries = d.run([_task_row(workspace, tid)])

    # The task did abandon durably.
    assert _task_row(workspace, tid)["status"] == "abandoned"
    digest = summaries[0]["digest"]
    assert summaries[0]["compressed"] is True
    # Line-1 is present verbatim and re-parses against the single-sourced grammar.
    # The default digest renders the abandoned notes as ``  notes: <line1>\n...``;
    # strip the ``notes:`` label prefix to recover the verbatim abandon line, then
    # confirm it still matches the single-sourced ABANDON_RE grammar.
    assert line1 in digest
    notes_line = next(ln for ln in digest.splitlines() if line1 in ln)
    recovered = notes_line.split("notes: ", 1)[1] if "notes: " in notes_line else notes_line
    assert ABANDON_RE.match(recovered) is not None
    assert ABANDON_RE.match(recovered).group("category") == "scope"
    # The big tail bulk is NOT carried verbatim.
    assert big_tail not in digest


# ── default_wave_digest is pure / deterministic / materially smaller ─────────


def test_default_wave_digest_deterministic_and_pure():
    """Same envelope list → byte-identical digest across two calls; the input is
    not mutated."""
    envs = [
        validate_envelope(
            _done_envelope(1, 1, notes_md=_BIG_NOTES),
            dispatched_task_id=1,
            dispatched_attempt=1,
        ),
        validate_envelope(
            _done_envelope(2, 1, notes_md="Z" * 5000),
            dispatched_task_id=2,
            dispatched_attempt=1,
        ),
    ]
    snapshot = [dict(e) for e in envs]
    out1 = default_wave_digest(envs)
    out2 = default_wave_digest(envs)
    assert out1 == out2  # deterministic
    assert envs == snapshot  # pure: inputs unmutated


def test_default_wave_digest_materially_smaller():
    """The digest is materially smaller (a measured byte reduction) than the
    verbatim reply bytes it summarizes."""
    env = validate_envelope(
        _done_envelope(
            7,
            3,
            notes_md=_BIG_NOTES,
            artifacts=[{"path": f"f{i}.py", "sha": "s"} for i in range(50)],
        ),
        dispatched_task_id=7,
        dispatched_attempt=3,
    )
    verbatim_bytes = len(env["notes_md"].encode("utf-8")) + len(repr(env["artifacts"]))
    digest_bytes = len(default_wave_digest([env]).encode("utf-8"))
    # A real reduction, not a vibe: at least 4x smaller given a >16KiB body.
    assert digest_bytes < verbatim_bytes
    assert digest_bytes * 4 < verbatim_bytes


# ── env threshold override (valid-or-ignore) ─────────────────────────────────


def test_garbage_env_threshold_ignored_uses_default(workspace):
    """A garbage ATELIER_COMPRESS_THRESHOLD is IGNORED — the module default is
    used, so a small wave is still not compressed."""
    tid = _seed_task(workspace, title="garbage-env", parallel_group=0)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt),
        clock=FakeClock(),
        env={"ATELIER_COMPRESS_THRESHOLD": "not-an-int"},
    )
    assert d._compress_threshold == COMPRESSION_THRESHOLD_BYTES
    summaries = d.run([_task_row(workspace, tid)])
    assert "compressed" not in summaries[0]


def test_valid_env_threshold_lowers_trigger(workspace):
    """A VALID small ATELIER_COMPRESS_THRESHOLD lowers the trigger so even a
    small wave compresses (proves the env override is wired, not just ignored)."""
    tid = _seed_task(workspace, title="low-thresh", parallel_group=0)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt),
        clock=FakeClock(),
        env={"ATELIER_COMPRESS_THRESHOLD": "1"},
        summarize_fn=lambda envs: "TINY-DIGEST",
    )
    assert d._compress_threshold == 1
    summaries = d.run([_task_row(workspace, tid)])
    assert summaries[0]["compressed"] is True
    assert summaries[0]["digest"] == "TINY-DIGEST"


# ── summarize_fn=None falls back to the deterministic default ────────────────


def test_summarize_fn_none_falls_back_to_default(workspace):
    """summarize_fn=None → the engine uses default_wave_digest, producing the
    same digest the standalone function would for the retained envelopes."""
    tid = _seed_task(workspace, title="fallback", parallel_group=0)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt, notes_md=_BIG_NOTES),
        clock=FakeClock(),
        summarize_fn=None,
    )
    summaries = d.run([_task_row(workspace, tid)])
    digest = summaries[0]["digest"]
    # It is the deterministic default's output, not a stub string.
    assert "task_id=" in digest and "status='done'" in digest
    assert "notes:" in digest


# ── B2 — env-gated caveman codec at the wave-summary digest sink ──────────────
#
# The codec runs ONLY on the model-bound `digest` string, never on the
# stored/validated envelopes. DEFAULT OFF (#1 invariant: OFF is byte-identical
# to the pre-codec default_wave_digest output). Each test below has a neuter
# tell so a silent revert of the codec call / gate turns the suite RED.

from scripts.caveman_codec import compress as _caveman_compress  # noqa: E402
from scripts.pm_dispatch import (  # noqa: E402
    CAVEMAN_COMPRESS_ENV,
    compress_reply_for_context,
)

# Compressible filler-heavy prose for a `done` envelope head (the first ~200
# chars survive into the default digest's `notes:` field, where caveman strips
# articles/filler). NO security / destructive / multi-step tripwords, so
# should_compress() returns True. Padded past the threshold so the wave
# compresses.
_CAVEMAN_HEAD = (
    "I will just basically refactor the auth layer and really tidy the imports, "
    "and the cache should perhaps be cleared too, please. "
)
_CAVEMAN_NOTES = _CAVEMAN_HEAD + ("filler prose " * 1500)

# Sentinel meaning "do not inject the env var at all" (env=None path).
_UNSET = object()


def _run_wave_capturing_digest(workspace, *, env, notes_md):
    """Run a single big wave and return its summary dict (compressed)."""
    tid = _seed_task(workspace, title="caveman", parallel_group=0)
    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt, notes_md=notes_md),
        clock=FakeClock(),
        env=env,
        # summarize_fn=None → pure deterministic default_wave_digest
    )
    summaries = d.run([_task_row(workspace, tid)])
    assert summaries[0]["compressed"] is True
    return d, summaries[0]


def test_b2_gate_parses_truthy_and_default_off():
    """The gate is parsed defensively from the INJECTED env (A8 — never
    os.environ). DEFAULT OFF; only explicit truthy markers enable."""

    def enabled(val):
        return WaveDispatcher(
            "x.db",
            spawn_fn=lambda t, a: None,
            poll_fn=lambda t, a: None,
            env=None if val is _UNSET else {CAVEMAN_COMPRESS_ENV: val},
        )._caveman_enabled

    for off in (_UNSET, "", "0", "false", "FALSE", "no", "off", " Off ", "maybe", "2"):
        assert enabled(off) is False, off
    for on in ("1", "true", "TRUE", "yes", "on", " On ", "Yes"):
        assert enabled(on) is True, on


def test_b2_off_digest_is_byte_identical_to_default(workspace):
    """#1 INVARIANT: with the gate unset (OFF, default), the digest is
    BYTE-IDENTICAL to the pure default_wave_digest output — no caveman mutation."""
    _d, summary = _run_wave_capturing_digest(workspace, env=None, notes_md=_CAVEMAN_NOTES)
    expected = default_wave_digest(_d._wave_envelopes)
    assert summary["digest"] == expected
    # Sanity: the codec WOULD have changed it if it had run (the neuter tell).
    assert _caveman_compress(expected, "full") != expected


def test_b2_on_digest_is_shorter_and_differs(workspace):
    """LIVE (ON): a large compressible wave produces a digest that DIFFERS from
    and is SHORTER than the pure (OFF-equivalent) digest. NEUTER: removing the
    codec call makes the ON digest equal the pure digest → this test goes RED.

    The pure OFF-equivalent baseline is recomputed from THIS run's own retained
    envelopes (`default_wave_digest`), so the task_id embedded in the digest
    matches — the only variable is whether the codec ran."""
    on_d, on_summary = _run_wave_capturing_digest(
        workspace, env={CAVEMAN_COMPRESS_ENV: "1"}, notes_md=_CAVEMAN_NOTES
    )
    pure = default_wave_digest(on_d._wave_envelopes)  # what OFF would have produced
    assert on_summary["digest"] != pure
    assert len(on_summary["digest"]) < len(pure)
    # The ON digest is EXACTLY the codec applied to the pure digest — proving the
    # codec (and the env flag) is the SOLE cause of the difference.
    assert on_summary["digest"] == _caveman_compress(pure, "full")


def test_b2_on_envelopes_stay_byte_exact(workspace):
    """ON must leave the RETAINED/validated envelopes byte-exact — the codec
    only ever touches the SEPARATE digest string, never the stored envelopes."""
    on_d, _summary = _run_wave_capturing_digest(
        workspace, env={CAVEMAN_COMPRESS_ENV: "1"}, notes_md=_CAVEMAN_NOTES
    )
    # The retained envelope's notes_md is the raw, uncompressed body.
    assert len(on_d._wave_envelopes) == 1
    assert on_d._wave_envelopes[0]["notes_md"] == _CAVEMAN_NOTES


def test_b2_neuter_flag_is_sole_cause_of_change(workspace):
    """NEUTER proof: flipping the env flag is the SOLE cause of the digest
    change. Each run's digest is compared against THAT run's own pure baseline
    (recomputed from its retained envelopes, so the embedded task_id matches):
    with the flag OFF the digest equals the pure default; with it ON the digest
    equals codec(pure). Only the flag differs."""
    off_d, off_summary = _run_wave_capturing_digest(
        workspace, env={CAVEMAN_COMPRESS_ENV: "0"}, notes_md=_CAVEMAN_NOTES
    )
    on_d, on_summary = _run_wave_capturing_digest(
        workspace, env={CAVEMAN_COMPRESS_ENV: "1"}, notes_md=_CAVEMAN_NOTES
    )
    off_base = default_wave_digest(off_d._wave_envelopes)
    on_base = default_wave_digest(on_d._wave_envelopes)
    assert off_summary["digest"] == off_base  # OFF = pure default (codec did NOT run)
    assert on_summary["digest"] == _caveman_compress(on_base, "full")  # ON = codec(default)
    assert on_summary["digest"] != on_base  # ON genuinely changed it


def test_b2_auto_clarity_passthrough_when_on(workspace):
    """Auto-clarity: with caveman ON, a wave whose digest carries a
    security/destructive marker yields a digest BYTE-IDENTICAL to OFF, because
    should_compress() refuses (verbatim pass-through)."""
    # A security tripword in the notes head propagates into the digest's
    # `notes:` field, so should_compress() trips → no compression even when ON.
    tripword_notes = ("Security warning: this exposes a credential and is irreversible. ") + (
        "filler prose " * 1500
    )
    on_d, on_summary = _run_wave_capturing_digest(
        workspace, env={CAVEMAN_COMPRESS_ENV: "1"}, notes_md=tripword_notes
    )
    # should_compress() trips on the security marker → digest is BYTE-IDENTICAL
    # to the pure default (codec refused), even though the gate is ON.
    pure = default_wave_digest(on_d._wave_envelopes)
    assert on_summary["digest"] == pure
    assert "Security warning" in on_summary["digest"]


def test_b2_helper_contract():
    """compress_reply_for_context: OFF / non-str / empty / tripwire → unchanged;
    otherwise codec(text)."""
    filler = "I will just basically restart the service and really tidy imports."
    # OFF.
    assert compress_reply_for_context(filler, enabled=False) == filler
    # non-str / empty.
    assert compress_reply_for_context("", enabled=True) == ""
    assert compress_reply_for_context(None, enabled=True) is None  # type: ignore[arg-type]
    # tripwire → verbatim.
    trip = "Security warning: rm -rf the directory is irreversible."
    assert compress_reply_for_context(trip, enabled=True) == trip
    # safe filler-heavy prose → compressed.
    out = compress_reply_for_context(filler, enabled=True)
    assert out != filler
    assert len(out) < len(filler)
    assert "just" not in out and "basically" not in out
