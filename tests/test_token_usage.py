# tests/test_token_usage.py
"""End-to-end tests for ``scripts/token_usage.py`` against REAL on-disk JSONL.

These tests drive the REAL public entrypoints (``daily_rollup`` /
``collect_usage_records`` / ``to_daily_rollup``) against ``tmp_path``-built
fixtures that model Claude Code's ACTUAL transcript shape on disk:

    <config_dir>/projects/<proj>/<session>.jsonl          # session transcript
    <config_dir>/projects/<proj>/subagents/agent-<id>.jsonl   # sidechain
    <config_dir>/projects/<proj>/subagents/agent-<id>.meta.json  # sibling meta

Mocks match reality: we never hand a fabricated file-list to the pure core —
every path below walks the same filesystem discovery + read that production
uses (``discover_transcripts`` → ``collect_usage_records``). The config dir is
pointed at the fixture root via the ``config_dir=`` argument and via the
``CLAUDE_CONFIG_DIR`` env override, both of which are exercised.

Stdlib + pytest only. Mirrors atelier CI: ``python3 -m pytest tests/ -q -p no:libtmux``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from scripts.token_usage import (
    _harden_token,
    collect_usage_records,
    daily_rollup,
    to_daily_rollup,
)

# ── fixture helpers (build REAL on-disk transcripts) ────────────────────────


def _config_root(tmp_path):
    """Create and return a ``<config_dir>/projects/<proj>`` directory tree."""
    proj = tmp_path / "projects" / "-home-user-myrepo"
    proj.mkdir(parents=True)
    return tmp_path, proj


def _write_jsonl(path, objs, *, mtime=None):
    """Write one JSONL file from a list of objects; optionally pin its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(o) if not isinstance(o, str) else o for o in objs) + "\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _assistant_line(
    *,
    msg_id=None,
    request_id=None,
    usage=None,
    model="claude-opus-4-8",
    timestamp="2026-06-20T12:00:00+00:00",
    session_id="sess-parent",
    is_sidechain=False,
):
    """Build one decoded ``assistant`` transcript object."""
    message = {"role": "assistant", "usage": usage if usage is not None else {}}
    if msg_id is not None:
        message["id"] = msg_id
    if model is not None:
        message["model"] = model
    obj = {
        "type": "assistant",
        "message": message,
        "timestamp": timestamp,
        "sessionId": session_id,
    }
    if request_id is not None:
        obj["requestId"] = request_id
    if is_sidechain:
        obj["isSidechain"] = True
    return obj


# ── (1) within-file streaming partials: ONE record, count == MAX (not sum) ───


def test_streaming_partials_collapse_to_one_record_with_max(tmp_path):
    root, proj = _config_root(tmp_path)
    # Same message.id + requestId, three growing partials in ONE file.
    lines = [
        _assistant_line(msg_id="m1", request_id="r1", usage={"output_tokens": 10}),
        _assistant_line(msg_id="m1", request_id="r1", usage={"output_tokens": 50}),
        _assistant_line(msg_id="m1", request_id="r1", usage={"output_tokens": 30}),
    ]
    _write_jsonl(proj / "session-a.jsonl", lines)

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    # MAX of the partials (50), NOT the sum (90).
    assert records[0].usage.output_tokens == 50


# ── (2) same key across TWO files w/ differing mtime: ONE record, not doubled ─


def test_duplicate_across_files_first_wins_not_double_counted(tmp_path):
    root, proj = _config_root(tmp_path)
    # The SAME message.id:requestId appears in an OLDER file and a NEWER copy
    # (e.g. a resumed-session file). Oldest-mtime occurrence wins.
    older = _write_jsonl(
        proj / "session-orig.jsonl",
        [_assistant_line(msg_id="dup", request_id="rr", usage={"output_tokens": 100})],
        mtime=1_000_000,
    )
    newer = _write_jsonl(
        proj / "session-resumed.jsonl",
        [_assistant_line(msg_id="dup", request_id="rr", usage={"output_tokens": 999})],
        mtime=2_000_000,
    )
    assert older.stat().st_mtime < newer.stat().st_mtime

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1  # not double-counted
    # First-wins by mtime → the ORIGINAL value, and definitely not the sum.
    assert records[0].usage.output_tokens == 100
    assert records[0].usage.output_tokens != 100 + 999


# ── (3) unkeyed line (no message.id): never dropped, always counted ──────────


def test_unkeyed_lines_are_always_counted(tmp_path):
    root, proj = _config_root(tmp_path)
    # No message.id → dedup_key is None → never deduped, both kept.
    lines = [
        _assistant_line(msg_id=None, usage={"output_tokens": 7}),
        _assistant_line(msg_id=None, usage={"output_tokens": 9}),
    ]
    _write_jsonl(proj / "session-unkeyed.jsonl", lines)

    records = collect_usage_records(config_dir=root)

    assert len(records) == 2
    assert sorted(r.usage.output_tokens for r in records) == [7, 9]


# ── (4) message.usage with iterations[] sub-array: TOP-LEVEL only ────────────


def test_iterations_subarray_is_ignored_no_double_count(tmp_path):
    root, proj = _config_root(tmp_path)
    usage = {
        "input_tokens": 100,
        "output_tokens": 40,
        # A nested per-step iterations array that MUST be ignored entirely.
        "iterations": [
            {"input_tokens": 50, "output_tokens": 20},
            {"input_tokens": 50, "output_tokens": 20},
        ],
    }
    _write_jsonl(proj / "session-iter.jsonl", [_assistant_line(msg_id="i1", usage=usage)])

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    # Top-level only — iterations sums (200/80) are NOT added in.
    assert records[0].usage.input_tokens == 100
    assert records[0].usage.output_tokens == 40


# ── (5) subagent sidechain + sibling meta.json ──────────────────────────────


def test_sidechain_record_included_with_meta_label_and_reparented_session(tmp_path):
    root, proj = _config_root(tmp_path)
    subagents = proj / "subagents"
    # The sidechain line carries its OWN sessionId (the parent session).
    line = _assistant_line(
        msg_id="sc1",
        usage={"output_tokens": 12},
        session_id="parent-session-uuid",
        is_sidechain=True,
    )
    _write_jsonl(subagents / "agent-7.jsonl", [line])
    # Sibling meta.json supplies the agent label.
    (subagents / "agent-7.meta.json").write_text(
        json.dumps({"agentType": "security-engineer-1"}), encoding="utf-8"
    )

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    rec = records[0]
    assert rec.is_sidechain is True
    assert rec.agent_label == "security-engineer-1"
    # session reparented to the line's OWN sessionId, not the path dir name.
    assert rec.session_id == "parent-session-uuid"
    assert rec.usage.output_tokens == 12


# ── (6) cache_creation TTL split fields are populated ───────────────────────


def test_cache_creation_ttl_split_is_surfaced(tmp_path):
    root, proj = _config_root(tmp_path)
    usage = {
        "cache_creation_input_tokens": 300,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 200,
            "ephemeral_1h_input_tokens": 100,
        },
    }
    _write_jsonl(proj / "session-cache.jsonl", [_assistant_line(msg_id="c1", usage=usage)])

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    rec = records[0]
    assert rec.cache_creation_5m == 200
    assert rec.cache_creation_1h == 100
    assert rec.usage.cache_creation_input_tokens == 300


# ── (7) _harden_token rejects a JSON bool as a count ────────────────────────


def test_harden_token_rejects_bool():
    # JSON `true` is not a count: rejected to 0, never coerced to int(True)==1.
    assert _harden_token(True) == (0, False)
    assert _harden_token(False) == (0, False)
    # Sanity: a real int still passes through.
    assert _harden_token(42) == (42, False)


# ── (8) daily_rollup(since=...) filters older days but RETAINS 'unknown' ─────


def test_since_filters_old_days_but_retains_unknown(tmp_path):
    root, proj = _config_root(tmp_path)
    # Use UTC-offset timestamps so day bucketing is deterministic regardless of
    # CI host tz (the +00:00 offset is embedded in each string).
    _write_jsonl(
        proj / "old.jsonl",
        [
            _assistant_line(
                msg_id="o1", usage={"output_tokens": 1}, timestamp="2026-06-10T12:00:00+00:00"
            )
        ],
    )
    _write_jsonl(
        proj / "new.jsonl",
        [
            _assistant_line(
                msg_id="n1", usage={"output_tokens": 2}, timestamp="2026-06-20T12:00:00+00:00"
            )
        ],
    )
    # A record with NO parseable timestamp → buckets under "unknown".
    _write_jsonl(
        proj / "unk.jsonl",
        [_assistant_line(msg_id="u1", usage={"output_tokens": 3}, timestamp=None)],
    )

    rows = daily_rollup(config_dir=root, since="2026-06-15")
    days = {row["day"] for row in rows}

    assert "2026-06-10" not in days  # older than `since` → dropped
    assert "2026-06-20" in days  # on/after `since` → kept
    assert "unknown" in days  # ALWAYS retained, never filtered by `since`


# ── (9) malformed JSONL line: skipped, walk continues, valid records remain ──


def test_malformed_line_skipped_walk_continues(tmp_path):
    root, proj = _config_root(tmp_path)
    # A broken line sandwiched between two valid assistant lines.
    objs = [
        _assistant_line(msg_id="g1", usage={"output_tokens": 5}),
        "{ this is not valid json ",
        _assistant_line(msg_id="g2", usage={"output_tokens": 6}),
    ]
    _write_jsonl(proj / "session-malformed.jsonl", objs)

    records = collect_usage_records(config_dir=root)

    assert len(records) == 2  # malformed dropped, both valid records survive
    assert sorted(r.usage.output_tokens for r in records) == [5, 6]


# ── (10) PIN the timezone so the local-tz day boundary is deterministic ──────


@pytest.fixture
def _fixed_tz():
    """Pin the process TZ to a fixed offset so local-day bucketing can't flake."""
    if not hasattr(time, "tzset"):  # pragma: no cover - non-POSIX guard
        pytest.skip("time.tzset() unavailable on this platform")
    prev = os.environ.get("TZ")
    # UTC-09:00 (Etc/GMT+9): a 23:30 UTC instant lands on the PREVIOUS local day.
    os.environ["TZ"] = "Etc/GMT+9"
    time.tzset()
    yield
    if prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prev
    time.tzset()


def test_local_tz_day_boundary_is_deterministic(tmp_path, _fixed_tz):
    root, proj = _config_root(tmp_path)
    # 2026-06-20T05:00:00Z, in Etc/GMT+9 (UTC-9), is 2026-06-19 20:00 local.
    _write_jsonl(
        proj / "boundary.jsonl",
        [
            _assistant_line(
                msg_id="b1", usage={"output_tokens": 4}, timestamp="2026-06-20T05:00:00+00:00"
            )
        ],
    )

    rows = daily_rollup(config_dir=root)

    assert len(rows) == 1
    # Crosses the local-tz day boundary backwards → 06-19, not 06-20.
    assert rows[0]["day"] == "2026-06-19"


# ── CLAUDE_CONFIG_DIR override drives the same discovery path ────────────────


def test_claude_config_dir_env_override(tmp_path, monkeypatch):
    root, proj = _config_root(tmp_path)
    _write_jsonl(
        proj / "session-env.jsonl", [_assistant_line(msg_id="e1", usage={"output_tokens": 8})]
    )

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    # No config_dir arg → resolves via CLAUDE_CONFIG_DIR.
    records = collect_usage_records()

    assert len(records) == 1
    assert records[0].usage.output_tokens == 8


# ── to_daily_rollup keeps the four categories split, sorted by (day, model) ──


def test_to_daily_rollup_splits_categories_and_models(tmp_path):
    root, proj = _config_root(tmp_path)
    _write_jsonl(
        proj / "session-roll.jsonl",
        [
            _assistant_line(
                msg_id="rA", model="claude-opus-4-8", usage={"input_tokens": 10, "output_tokens": 1}
            ),
            _assistant_line(
                msg_id="rB",
                model="claude-sonnet-4-6",
                usage={"input_tokens": 20, "output_tokens": 2},
            ),
        ],
    )

    rows = to_daily_rollup(collect_usage_records(config_dir=root))

    models = {row["model"] for row in rows}
    assert models == {"claude-opus-4-8", "claude-sonnet-4-6"}
    for row in rows:
        # Schema carries all four channels split out.
        assert set(row) >= {
            "day",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "model",
        }


# ── transcripts/ tree is ALSO walked (not just projects/) ────────────────────


def test_transcripts_tree_is_also_walked(tmp_path):
    # The verified reference walks BOTH <base>/projects/ and <base>/transcripts/.
    # A record living only under transcripts/ must still be discovered (else a
    # silent under-count vs the contract).
    tdir = tmp_path / "transcripts" / "-home-user-myrepo"
    _write_jsonl(
        tdir / "session-t.jsonl",
        [_assistant_line(msg_id="t1", usage={"output_tokens": 11})],
    )

    records = collect_usage_records(config_dir=tmp_path)

    assert len(records) == 1
    assert records[0].usage.output_tokens == 11


# ── sidechain WITHOUT sessionId: reparent falls back to the path dir ─────────


def test_sidechain_path_fallback_reparent_when_no_session_id(tmp_path):
    root, proj = _config_root(tmp_path)
    subagents = proj / "subagents"
    # A sidechain line carrying NO sessionId → session reparents to the parent
    # session dir name derived from the path (<proj>/subagents/agent-*.jsonl).
    line = _assistant_line(
        msg_id="scf1",
        usage={"output_tokens": 3},
        session_id=None,
        is_sidechain=True,
    )
    _write_jsonl(subagents / "agent-9.jsonl", [line])

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    rec = records[0]
    assert rec.is_sidechain is True
    # parent of "subagents" is the project dir → that name is the reparented id.
    assert rec.session_id == "-home-user-myrepo"


# ── TTL split fields MAX-merge across streaming partials (same dedup key) ─────


def test_ttl_split_max_merges_across_partials(tmp_path):
    root, proj = _config_root(tmp_path)
    # Two partials of the SAME message.id:requestId with GROWING TTL-split values;
    # the per-field MAX merge must surface the larger of each, not the first/sum.
    lines = [
        _assistant_line(
            msg_id="ttl",
            request_id="rttl",
            usage={
                "cache_creation_input_tokens": 100,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 50,
                    "ephemeral_1h_input_tokens": 10,
                },
            },
        ),
        _assistant_line(
            msg_id="ttl",
            request_id="rttl",
            usage={
                "cache_creation_input_tokens": 300,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200,
                    "ephemeral_1h_input_tokens": 80,
                },
            },
        ),
    ]
    _write_jsonl(proj / "session-ttl.jsonl", lines)

    records = collect_usage_records(config_dir=root)

    assert len(records) == 1
    rec = records[0]
    # MAX of each TTL bucket across the partials (not the first, not the sum).
    assert rec.cache_creation_5m == 200
    assert rec.cache_creation_1h == 80
    assert rec.usage.cache_creation_input_tokens == 300
