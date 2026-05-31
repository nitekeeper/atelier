# tests/test_team_meeting.py
"""Unit tests for scripts/team_meeting.py (atelier#64 AI-2).

The agent-team-mode plan-phase meeting thread:

* FAN-OUT — one bridge_send per teammate per logical message, sharing a
  send-call identity (base_key) but DISTINCT per-recipient DB idempotency keys
  (so the (team_id, idempotency_key) UNIQUE index does not collapse the
  fan-out into a single row). The mocked bridge sender records every call.
* §7.2 backstops — wall-clock (injected clock) and message-count (200 distinct
  base_keys) tested INDEPENDENTLY (livelock vs flooding). Backstop-forced
  termination flags the minutes PARTIAL, never clean consensus.
* _mtype decode — fail-soft: malformed/unknown/forged markers degrade to None
  (ordinary reply, no privileged op) — NN2.
* causal_ref ordering — every member rebuilds the SAME ordered transcript.

Determinism: NO wall-clock / real sleeps. The clock is an injected callable; the
bridge sender is an injected mock.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts import bridge_send, team_meeting
from scripts.migrate import apply_migrations
from scripts.team_meeting import (
    MESSAGE_COUNT_CAP,
    WALL_CLOCK_CAP_S,
    MeetingBackstopExceeded,
    MeetingState,
    declare_done,
    decode_mtype,
    derive_idem,
    post_message,
    reconstruct_thread,
)

# ── Mocked bridge sender ────────────────────────────────────────────────────


class FakeBridge:
    """Records send() calls and allocates a per-recipient monotonic seq,
    deduping on (team_id, idempotency_key) exactly like the real writer.

    This mirrors bridge_send.send's idempotency contract precisely so the
    fan-out's per-recipient distinct-key requirement is actually exercised:
    if team_meeting wrongly reused ONE key across recipients, the second
    recipient would dedupe to the first's seq (collapsing the fan-out), and
    the per-recipient row assertions below would fail.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._seq: dict[tuple[str, str], int] = {}
        self._by_idem: dict[tuple[str, str], dict[str, Any]] = {}

    def send(self, db_path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"db_path": db_path, **kw})
        team_id = kw["team_id"]
        recipient = kw["recipient"]
        idem = kw.get("idempotency_key")
        if idem is not None and (team_id, idem) in self._by_idem:
            prior = self._by_idem[(team_id, idem)]
            return {"seq": prior["seq"], "deduped": True, "persona_snapshot_id": 1}
        nxt = self._seq.get((team_id, recipient), 0) + 1
        self._seq[(team_id, recipient)] = nxt
        rec = {"seq": nxt, "deduped": False, "persona_snapshot_id": 1}
        if idem is not None:
            self._by_idem[(team_id, idem)] = rec
        return rec


class FakeClock:
    """Injectable monotonic clock — returns whatever epoch second it's set to."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── Fan-out ─────────────────────────────────────────────────────────────────


def test_fan_out_writes_one_row_per_recipient_distinct_keys() -> None:
    """A single logical message fans out to N recipients via N sends, each
    with a DISTINCT idempotency key — so the fan-out is N real rows, not one
    deduped row. The base_key is the single send-call identity (counted once)."""
    bridge = FakeBridge()
    clock = FakeClock()
    state = MeetingState()
    recipients = ["planner", "backend-engineer-1", "security-engineer-1"]

    out = post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=recipients,
        body={"text": "let us begin"},
        base_key="BASEKEY-MEETING-MSG-0001",
        sender=bridge.send,
        clock=clock,
    )

    # One send per recipient — true fan-out, no broadcast sentinel recipient.
    assert len(bridge.calls) == 3
    assert {c["recipient"] for c in bridge.calls} == set(recipients)
    # DISTINCT per-recipient idempotency keys (else the FakeBridge would dedupe).
    idems = [c["idempotency_key"] for c in bridge.calls]
    assert len(set(idems)) == 3
    assert all(len(i) == bridge_send.ULID_LEN for i in idems)
    # None deduped — every recipient got its own row.
    assert out["message_count"] == 1
    # The send-call identity is counted once regardless of fan-out width.
    assert state.message_count == 1


def test_fan_out_uses_reply_kind_and_minted_mtype() -> None:
    """Meeting messages ride kind='reply' with a framework-minted _mtype —
    no new bridge_messages.kind value (zero-migration WIRE-REP)."""
    bridge = FakeBridge()
    state = MeetingState()
    post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a", "b"],
        body={"text": "x"},
        base_key="K1",
        sender=bridge.send,
        clock=FakeClock(),
    )
    for c in bridge.calls:
        assert c["kind"] == "reply"
        decoded = json.loads(c["payload"])
        assert decoded[bridge_send.RESERVED_MTYPE_KEY] == "team_meeting"


def test_persona_gap_rides_the_meeting_fan_out() -> None:
    """Persona-gap CAPTURE rides the meeting fan-out as _mtype='persona_gap'
    (distinct from the audit-ledger escalation, AI-2-escalation)."""
    bridge = FakeBridge()
    state = MeetingState()
    post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a", "b"],
        body={"gap": "need a security-engineer for the auth work"},
        base_key="GAP-1",
        mtype="persona_gap",
        sender=bridge.send,
        clock=FakeClock(),
    )
    assert json.loads(bridge.calls[0]["payload"])[bridge_send.RESERVED_MTYPE_KEY] == "persona_gap"


def test_derive_idem_is_deterministic_and_per_recipient() -> None:
    """Replay-safety: the derived key is stable in (base_key, recipient) and
    differs across recipients (so the fan-out rows don't collide)."""
    assert derive_idem("BASE", "alice") == derive_idem("BASE", "alice")
    assert derive_idem("BASE", "alice") != derive_idem("BASE", "bob")
    assert derive_idem("BASE", "alice") != derive_idem("OTHER", "alice")
    assert len(derive_idem("BASE", "alice")) == bridge_send.ULID_LEN
    assert all(ch in team_meeting._CROCKFORD for ch in derive_idem("BASE", "alice"))


def test_replay_dedupes_per_recipient() -> None:
    """Posting the same base_key twice re-derives the same per-recipient keys,
    so each recipient dedupes to its original row (no duplicate fan-out)."""
    bridge = FakeBridge()
    state = MeetingState()
    kw = {
        "db_path": "db",
        "team_id": "T1",
        "sender_id": "planner",
        "recipients": ["a", "b"],
        "body": {"text": "x"},
        "base_key": "REPLAY-1",
        "sender": bridge.send,
        "clock": FakeClock(),
    }
    first = post_message(state=state, **kw)  # type: ignore[arg-type]
    second = post_message(state=state, **kw)  # type: ignore[arg-type]
    # base_key counted once across the replay.
    assert state.message_count == 1
    # Same seqs returned both times (deduped on the second).
    assert first["seqs"] == second["seqs"]


# ── §7.2 backstops — tested INDEPENDENTLY ───────────────────────────────────


def test_backstop_message_count_independent_of_wall_clock() -> None:
    """FLOODING: 200 distinct send-calls is the cap; the 201st raises with
    reason='message_count' and flags the meeting PARTIAL — even though the
    clock never advances (wall-clock is NOT the trigger here)."""
    bridge = FakeBridge()
    clock = FakeClock(t=5000.0)  # frozen — wall-clock can never fire
    state = MeetingState()
    for i in range(MESSAGE_COUNT_CAP):
        post_message(
            state=state,
            db_path="db",
            team_id="T1",
            sender_id="planner",
            recipients=["a"],
            body={"i": i},
            base_key=f"MSG-{i:04d}",
            sender=bridge.send,
            clock=clock,
        )
    assert state.message_count == MESSAGE_COUNT_CAP
    assert state.partial is False
    with pytest.raises(MeetingBackstopExceeded) as exc:
        post_message(
            state=state,
            db_path="db",
            team_id="T1",
            sender_id="planner",
            recipients=["a"],
            body={"i": MESSAGE_COUNT_CAP},
            base_key=f"MSG-{MESSAGE_COUNT_CAP:04d}",
            sender=bridge.send,
            clock=clock,
        )
    assert exc.value.reason == "message_count"
    assert state.partial is True
    assert state.partial_reason == "message_count"


def test_backstop_wall_clock_independent_of_message_count() -> None:
    """LIVELOCK: once 60 min elapse from the first message, the next post
    raises with reason='wall_clock' and flags PARTIAL — even though only a
    handful of messages were sent (message-count is NOT the trigger here)."""
    bridge = FakeBridge()
    clock = FakeClock(t=1000.0)
    state = MeetingState()
    post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a"],
        body={"text": "first"},
        base_key="K1",
        sender=bridge.send,
        clock=clock,
    )
    # Advance the injected clock just past the cap; only 1 message so far.
    clock.t = 1000.0 + WALL_CLOCK_CAP_S
    with pytest.raises(MeetingBackstopExceeded) as exc:
        post_message(
            state=state,
            db_path="db",
            team_id="T1",
            sender_id="planner",
            recipients=["a"],
            body={"text": "too late"},
            base_key="K2",
            sender=bridge.send,
            clock=clock,
        )
    assert exc.value.reason == "wall_clock"
    assert state.message_count == 1  # the spiralling message was NOT written
    assert state.partial is True
    assert state.partial_reason == "wall_clock"


def test_backstop_forced_done_flags_partial_not_clean_consensus() -> None:
    """On backstop, the planner can STILL declare the meeting done (declare_done
    bypasses the pre-check) and the minutes are flagged PARTIAL."""
    bridge = FakeBridge()
    clock = FakeClock(t=1000.0)
    state = MeetingState()
    post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a"],
        body={"text": "x"},
        base_key="K1",
        sender=bridge.send,
        clock=clock,
    )
    clock.t = 1000.0 + WALL_CLOCK_CAP_S
    with pytest.raises(MeetingBackstopExceeded):
        post_message(
            state=state,
            db_path="db",
            team_id="T1",
            sender_id="planner",
            recipients=["a"],
            body={"text": "spiral"},
            base_key="K2",
            sender=bridge.send,
            clock=clock,
        )
    done = declare_done(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a"],
        base_key="DONE",
        summary={"decisions": []},
        sender=bridge.send,
        clock=clock,
    )
    assert done["minutes_partial"] is True
    assert done["partial_reason"] == "wall_clock"
    # The meeting_done message carries _mtype='meeting_done' + partial flag.
    done_payload = json.loads(bridge.calls[-1]["payload"])
    assert done_payload[bridge_send.RESERVED_MTYPE_KEY] == "meeting_done"
    assert done_payload["partial"] is True


def test_clean_consensus_meeting_done_not_partial() -> None:
    """A meeting that ends WITHOUT a backstop declares done with partial=False
    (clean consensus)."""
    bridge = FakeBridge()
    clock = FakeClock()
    state = MeetingState()
    post_message(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a"],
        body={"text": "agreed"},
        base_key="K1",
        sender=bridge.send,
        clock=clock,
    )
    done = declare_done(
        state=state,
        db_path="db",
        team_id="T1",
        sender_id="planner",
        recipients=["a"],
        base_key="DONE",
        summary={"decisions": ["ship it"]},
        sender=bridge.send,
        clock=clock,
    )
    assert done["minutes_partial"] is False
    assert done["partial_reason"] is None


# ── _mtype decode (NN2 fail-soft) ───────────────────────────────────────────


def test_decode_mtype_honors_in_set_marker() -> None:
    payload = bridge_send.encode_payload({"text": "x"}, mtype="propose_role")
    assert decode_mtype(payload) == "propose_role"


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps(["a", "list", "not", "an", "object"]),
        json.dumps({"text": "no mtype key here"}),
        json.dumps({"_mtype": 12345}),  # non-string
        json.dumps({"_mtype": "totally_unknown_type"}),  # out-of-set
        json.dumps("a bare string"),
    ],
)
def test_decode_mtype_fail_soft_to_none(payload: str) -> None:
    """NN2: malformed / unknown / forged markers degrade to None — the reader
    treats the message as an ordinary reply, NO privileged op, NO implicit ack."""
    assert decode_mtype(payload) is None


# ── causal_ref ordering — every member rebuilds the SAME transcript ─────────


def _seed_channel(meeting_seqs: list[int], created_ats: list[str], private: list[dict]):
    """Build one member's observable view: a meeting sub-thread (each message
    replies to the previous via causal_ref) interleaved with private messages.

    Returns rows as bridge_read-shaped dicts."""
    rows: list[dict] = []
    prev: int | None = None
    for seq, ts, logical in zip(meeting_seqs, created_ats, ["m0", "m1", "m2", "m3"], strict=True):
        rows.append({"seq": seq, "causal_ref": prev, "created_at": ts, "logical": logical})
        prev = seq
    rows.extend(private)
    return rows


def test_reconstruct_thread_same_order_across_members() -> None:
    """Two members observe DIFFERENT seq numbers (independent per-recipient
    seq streams) and DIFFERENT interleaved private messages, but rebuild the
    SAME ordered sequence of meeting messages via causal_ref."""
    # Member A's channel: meeting messages at seqs 1,2,3,4; a private msg at 5.
    view_a = _seed_channel(
        meeting_seqs=[1, 2, 3, 4],
        created_ats=["t1", "t2", "t3", "t4"],
        private=[{"seq": 5, "causal_ref": None, "created_at": "t1b", "logical": "privA"}],
    )
    # Member B's channel: SAME meeting messages but at seqs 10,11,12,13
    # (its own per-recipient stream), plus a different private msg at 9.
    view_b = _seed_channel(
        meeting_seqs=[10, 11, 12, 13],
        created_ats=["t1", "t2", "t3", "t4"],
        private=[{"seq": 9, "causal_ref": None, "created_at": "t0", "logical": "privB"}],
    )

    order_a = [m["logical"] for m in reconstruct_thread(view_a) if m["logical"].startswith("m")]
    order_b = [m["logical"] for m in reconstruct_thread(view_b) if m["logical"].startswith("m")]

    assert order_a == ["m0", "m1", "m2", "m3"]
    assert order_a == order_b


def test_reconstruct_thread_does_not_mutate_input() -> None:
    rows = [
        {"seq": 1, "causal_ref": None, "created_at": "t1", "logical": "m0"},
        {"seq": 2, "causal_ref": 1, "created_at": "t2", "logical": "m1"},
    ]
    snapshot = list(rows)
    reconstruct_thread(rows)
    assert rows == snapshot


# ── Persona-gap ONE-SHOT escalation (atelier#64 AI-2-escalation; §7.3) ──────
#
# CAPTURE (the persona_gap fan-out above) and ESCALATION (the audit ledger) are
# DISTINCT writes — the exactly-once guard counts LEDGER rows, NOT transcript
# mentions. These tests are DB-backed (Local mode) because the latch reads the
# real team_audit_log.

_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def team_workspace(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), _MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), _MIGRATIONS_DIR / "local-only")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
        ("T1", "P1", "team-lead", "active"),
    )
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db)}


def _audit_count(db: str, event_type: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM team_audit_log WHERE event_type = ?", (event_type,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_escalation_fires_exactly_once_across_recurring_gap(team_workspace) -> None:
    """AC2/NN3: the same gap recurring across N rounds escalates EXACTLY once —
    assert the LEDGER has exactly 1 row (== 1, not >= 1), proving the guard
    counts ledger rows not transcript mentions."""
    db = team_workspace["db"]
    first = team_meeting.escalate_persona_gap(
        team_id="T1", gap_id="need-security-engineer", description="auth work, no sec role"
    )
    assert first is not None
    # Re-raise the SAME gap across several more "rounds".
    for _ in range(5):
        again = team_meeting.escalate_persona_gap(
            team_id="T1", gap_id="need-security-engineer", description="still no sec role"
        )
        assert again is None  # never re-escalates
    assert _audit_count(db, "persona_gap_escalation") == 1


def test_distinct_gaps_escalate_independently(team_workspace) -> None:
    """The latch is per-(team, gap) — two DIFFERENT gaps each escalate once."""
    team_meeting.escalate_persona_gap(team_id="T1", gap_id="gap-A", description="a")
    team_meeting.escalate_persona_gap(team_id="T1", gap_id="gap-B", description="b")
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 2


def test_unresolved_escalation_writes_postmortem_and_stops(team_workspace) -> None:
    """AC2: when the human never resolves, the planner writes a
    meeting_failure_postmortem and STOPS — zero re-escalation."""
    db = team_workspace["db"]
    team_meeting.escalate_persona_gap(team_id="T1", gap_id="gap-A", description="a")
    # No human resolution → write the postmortem (§7.3 terminal state).
    pm = team_meeting.record_meeting_failure_postmortem(
        team_id="T1", gap_id="gap-A", reason="no human resolution; cannot synthesize task list"
    )
    assert pm["event_type"] == "meeting_failure_postmortem"
    # STOP: a subsequent escalate attempt for the same gap still does NOT
    # re-escalate (the latch holds), and the escalation ledger stays at 1.
    assert team_meeting.escalate_persona_gap(team_id="T1", gap_id="gap-A", description="a") is None
    assert _audit_count(db, "persona_gap_escalation") == 1
    assert _audit_count(db, "meeting_failure_postmortem") == 1


def test_capture_and_escalation_are_distinct_writes(team_workspace) -> None:
    """The persona_gap CAPTURE (meeting fan-out, mocked here) and the ESCALATION
    (audit ledger) are independent: many capture mentions, exactly one ledger
    escalation."""
    db = team_workspace["db"]
    bridge = FakeBridge()
    state = MeetingState()
    # Capture the gap several times in the transcript (recurring mentions).
    for i in range(3):
        post_message(
            state=state,
            db_path=db,
            team_id="T1",
            sender_id="planner",
            recipients=["a", "b"],
            body={"gap": "need security-engineer", "round": i},
            base_key=f"GAP-MENTION-{i}",
            mtype="persona_gap",
            sender=bridge.send,
            clock=FakeClock(),
        )
        # The escalation guard is unaffected by transcript volume.
        team_meeting.escalate_persona_gap(
            team_id="T1", gap_id="need-security-engineer", description="m"
        )
    # 3 logical capture messages fanned out to 2 recipients = 6 bridge calls.
    assert len(bridge.calls) == 6
    # ... but EXACTLY ONE ledger escalation row.
    assert _audit_count(db, "persona_gap_escalation") == 1
