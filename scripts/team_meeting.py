# scripts/team_meeting.py
"""Agent-team-mode plan-phase MEETING thread (atelier#64 AI-2).

NOTE: this is the *team meeting thread* — distinct from ``scripts/meetings.py``,
which owns human meeting-records (``.ai/meetings/*.md`` + the
``meeting_minutes`` table). Do NOT conflate the two. This module never touches
``meetings.py`` or its tables.

## Wire shape (design §7.2)

The plan-phase meeting is a TEAM-WIDE-visible thread carried over the existing
``bridge_messages`` transport. Per the WIRE-REP decision (atelier#64), meeting
messages ride ``kind='reply'`` and carry a framework-minted ``_mtype``
discriminator (``team_meeting`` / ``persona_gap`` / ``meeting_done`` …) — no
new ``kind`` enum value, no migration, no ``user_version`` bump.

### FAN-OUT, not a broadcast sentinel

``bridge_messages`` has a per-(team_id, recipient) sequence + delivery cursor.
There is NO broadcast recipient: a single sentinel recipient would collide on
the per-recipient ``bridge_delivery`` cursor and starve real readers. So
team-wide visibility is achieved by explicit FAN-OUT — one point-to-point
``bridge_send`` per teammate. This is TM-003-compliant ("explicit fan-out
only"); no TM-003 amendment is needed.

The N fanned-out rows for one logical meeting message share ONE *send-call
identity* (the ``base_key``). But the DB idempotency index is
``(team_id, idempotency_key)`` UNIQUE — a literally-shared key would dedupe all
but the first recipient down to a single row. So each fan-out row carries a
DISTINCT *derived* per-recipient idempotency key (``_derive_idem(base, rcpt)``,
a deterministic 26-char Crockford-base32 token). Replay-safety holds: re-posting
the same ``base_key`` re-derives the same per-recipient keys, so each recipient
dedupes to its original row.

### Counting (the §7.2 200-cap)

The message-count backstop counts DISTINCT *send-call identities* (``base_key``s
— logical meeting messages), NOT fanned-out rows. ``post_message`` returns the
``base_key`` it used; callers accumulate them in a ``MeetingState`` whose
``message_count`` is ``len(distinct base_keys)``.

### Causal ordering (design — existing ``causal_ref`` column)

Each meeting message replies to the previous one via ``causal_ref`` (the seq it
follows). Because every recipient's per-recipient seq stream is independent, the
canonical thread order is reconstructed from ``causal_ref`` adjacency +
``created_at`` tiebreak — every reader rebuilds the SAME ordered transcript
regardless of which recipient channel they read (``reconstruct_thread``).

## §7.2 backstops (Python, NOT DB) with an INJECTABLE clock

Two independent caps force termination on a spiral; on either, the meeting is
flagged PARTIAL (never clean consensus):

* **Wall-clock**: ``WALL_CLOCK_CAP_S`` (default 3600 = 60 min) measured from the
  first meeting message's ``created_at``.
* **Message-count**: ``MESSAGE_COUNT_CAP`` (default 200) distinct ``base_key``s.

Determinism: the clock is injected (``clock`` callable returning epoch
seconds) — no ``time.time()`` / ``datetime.now`` on the hot path, no real
sleeps in tests.

The actual ``bridge_send.send`` writer is also injectable (``sender``) so unit
tests drive the fan-out against a mocked bridge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scripts import backend, bridge_send

# ── Persona-gap escalation event types (atelier#64 AI-2-escalation) ─────────
#
# CAPTURE (the meeting transcript fan-out, _mtype='persona_gap') and ESCALATION
# (this audit ledger) are DISTINCT writes so the exactly-once guard counts
# LEDGER rows, not transcript mentions. The escalation row is the no-retry
# LATCH; the postmortem row is the terminal state when the human never resolves.
PERSONA_GAP_ESCALATION_EVENT_TYPE = "persona_gap_escalation"
MEETING_FAILURE_POSTMORTEM_EVENT_TYPE = "meeting_failure_postmortem"

# ── Backstop caps (design §7.2) ─────────────────────────────────────────────

#: Wall-clock cap, seconds. 60 minutes (design default). Measured from the
#: first meeting message's created_at via the injected clock.
WALL_CLOCK_CAP_S: float = 60.0 * 60.0

#: Message-count cap — DISTINCT send-call identities (base_keys / logical
#: meeting messages), NOT fanned-out rows (design §7.2).
MESSAGE_COUNT_CAP: int = 200

#: Crockford base32 alphabet (no I, L, O, U) — the ULID alphabet. Used to
#: render the derived per-recipient idempotency keys so they pass
#: bridge_send._validate_idem (exactly ULID_LEN Crockford-base32 chars).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: A bridge sender callable: same positional/keyword contract as
#: bridge_send.send. Injected so tests can drive the fan-out with a mock.
Sender = Callable[..., dict[str, Any]]

#: A clock callable returning epoch seconds (float). Injected for determinism.
Clock = Callable[[], float]


class MeetingError(RuntimeError):
    """Base class for team-meeting failures."""


class MeetingBackstopExceeded(MeetingError):
    """Raised when a backstop cap (wall-clock or message-count) is hit.

    Carries ``reason`` ('wall_clock' | 'message_count') so the caller can flag
    the minutes PARTIAL with the specific trigger."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ── Derived per-recipient idempotency key ───────────────────────────────────


def _to_crockford(digest: bytes, length: int) -> str:
    """Render ``digest`` bytes as a fixed-``length`` Crockford-base32 string.

    Deterministic, so a replay re-derives the identical key — the fan-out's
    per-recipient dedupe relies on this. Not a real ULID (no time prefix); we
    only need a stable, valid-alphabet, fixed-length token for the
    ``(team_id, idempotency_key)`` UNIQUE index.
    """
    n = int.from_bytes(digest, "big")
    out: list[str] = []
    for _ in range(length):
        out.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def derive_idem(base_key: str, recipient: str) -> str:
    """Derive the DISTINCT per-recipient idempotency key for a fan-out row.

    A logical meeting message has one ``base_key`` (the send-call identity used
    for counting); each fanned-out recipient row gets its own DB key so the
    ``(team_id, idempotency_key)`` UNIQUE index does not collapse the fan-out
    into a single row. Deterministic in ``(base_key, recipient)`` for replay
    safety.
    """
    digest = hashlib.sha256(f"{base_key}\x00{recipient}".encode()).digest()
    return _to_crockford(digest, bridge_send.ULID_LEN)


# ── Meeting state (count + wall-clock anchor) ───────────────────────────────


@dataclass
class MeetingState:
    """In-Python accounting for one meeting channel's backstops.

    * ``base_keys`` — the set of DISTINCT send-call identities posted so far;
      ``message_count`` is ``len(base_keys)`` (fanned-out rows are NOT counted).
    * ``first_message_at`` — epoch seconds of the first message, the wall-clock
      anchor. ``None`` until the first post.
    * ``partial`` / ``partial_reason`` — set when a backstop forces termination,
      so the minutes are flagged PARTIAL (never clean consensus).
    """

    base_keys: set[str] = field(default_factory=set)
    first_message_at: float | None = None
    partial: bool = False
    partial_reason: str | None = None

    @property
    def message_count(self) -> int:
        return len(self.base_keys)


# ── Fan-out post ─────────────────────────────────────────────────────────────


def post_message(
    *,
    state: MeetingState,
    db_path: str,
    team_id: str,
    sender_id: str,
    recipients: Sequence[str],
    body: dict[str, Any],
    base_key: str,
    mtype: str = "team_meeting",
    causal_ref: int | None = None,
    sender: Sender = bridge_send.send,
    clock: Clock,
) -> dict[str, Any]:
    """Fan a single logical meeting message out to every teammate.

    Returns ``{"base_key", "seqs": {recipient: seq}, "message_count"}``.

    Backstops are checked BEFORE the write (fail-closed): a post that would be
    the (cap+1)-th distinct send-call, or one issued past the wall-clock cap,
    raises :class:`MeetingBackstopExceeded` and flags ``state`` PARTIAL — the
    spiralling message is never written. The planner's forced ``meeting_done``
    (also a backstop-driven post) is the caller's responsibility AFTER catching
    this, posted with ``_check_backstops`` bypassed via ``force=True`` so the
    PARTIAL-state minutes can still be declared.

    The framework mints ``_mtype`` (forgery-safe) via
    ``bridge_send.encode_payload`` — caller content in ``body`` may NOT set the
    reserved key.
    """
    now = clock()
    is_new_call = base_key not in state.base_keys
    _check_backstops(state, now=now, adding_new_call=is_new_call)

    if state.first_message_at is None:
        state.first_message_at = now

    payload = bridge_send.encode_payload(body, mtype=mtype)

    seqs: dict[str, int] = {}
    for rcpt in recipients:
        result = sender(
            db_path,
            team_id=team_id,
            recipient=rcpt,
            sender_id=sender_id,
            kind="reply",
            payload=payload,
            idempotency_key=derive_idem(base_key, rcpt),
            causal_ref=causal_ref,
        )
        seqs[rcpt] = int(result["seq"])

    state.base_keys.add(base_key)
    return {"base_key": base_key, "seqs": seqs, "message_count": state.message_count}


def _check_backstops(state: MeetingState, *, now: float, adding_new_call: bool) -> None:
    """Raise + flag PARTIAL if a backstop cap is hit (design §7.2).

    Wall-clock and message-count are INDEPENDENT triggers. Re-posting an
    already-counted base_key (``adding_new_call=False``, e.g. a replay) does not
    advance the count cap. Backstop-forced termination is recorded on ``state``
    so the minutes render PARTIAL.
    """
    if state.first_message_at is not None:
        elapsed = now - state.first_message_at
        if elapsed >= WALL_CLOCK_CAP_S:
            state.partial = True
            state.partial_reason = "wall_clock"
            raise MeetingBackstopExceeded(
                "wall_clock",
                f"meeting wall-clock cap of {WALL_CLOCK_CAP_S:.0f}s exceeded "
                f"(elapsed {elapsed:.0f}s) — planner must declare the meeting "
                f"done with PARTIAL state (design §7.2).",
            )
    if adding_new_call and state.message_count >= MESSAGE_COUNT_CAP:
        state.partial = True
        state.partial_reason = "message_count"
        raise MeetingBackstopExceeded(
            "message_count",
            f"meeting message-count cap of {MESSAGE_COUNT_CAP} distinct "
            f"send-calls exceeded — planner must declare the meeting done with "
            f"PARTIAL state (design §7.2).",
        )


def declare_done(
    *,
    state: MeetingState,
    db_path: str,
    team_id: str,
    sender_id: str,
    recipients: Sequence[str],
    base_key: str,
    summary: dict[str, Any] | None = None,
    causal_ref: int | None = None,
    sender: Sender = bridge_send.send,
    clock: Clock,
) -> dict[str, Any]:
    """Post the planner's ``_mtype='meeting_done'`` declaration (design §7.2).

    Bypasses the backstop pre-check (``force``) so a backstop-forced
    termination can ALWAYS be declared — otherwise a meeting that spiralled to
    the cap could never be closed. The returned ``minutes_partial`` echoes
    ``state.partial`` so the caller flags the minutes correctly.
    """
    body: dict[str, Any] = {"summary": summary or {}, "partial": state.partial}
    if state.partial_reason is not None:
        body["partial_reason"] = state.partial_reason

    now = clock()
    if state.first_message_at is None:
        state.first_message_at = now

    payload = bridge_send.encode_payload(body, mtype="meeting_done")
    seqs: dict[str, int] = {}
    for rcpt in recipients:
        result = sender(
            db_path,
            team_id=team_id,
            recipient=rcpt,
            sender_id=sender_id,
            kind="reply",
            payload=payload,
            idempotency_key=derive_idem(base_key, rcpt),
            causal_ref=causal_ref,
        )
        seqs[rcpt] = int(result["seq"])
    # meeting_done is a control message, not a deliberation turn — it does NOT
    # count toward the message-count cap.
    return {
        "base_key": base_key,
        "seqs": seqs,
        "minutes_partial": state.partial,
        "partial_reason": state.partial_reason,
    }


# ── _mtype decode (reader side) ─────────────────────────────────────────────


def decode_mtype(payload: str) -> str | None:
    """Return the framework-minted ``_mtype`` for a wire payload, or ``None``.

    Fail-soft (NN2): a malformed / non-JSON / non-object payload, a missing
    ``_mtype``, a non-string ``_mtype``, or an UNKNOWN (out-of-set) ``_mtype``
    all return ``None`` — the reader treats the message as an ordinary 'reply'
    and performs NO privileged op (no implicit ack, no escalation).

    Decode is STRUCTURAL ONLY: an in-set discriminator is *dispatched* as a
    meeting message, NOT *trusted* as authentic. Authenticity is enforced at
    the write path (``bridge_send.encode_payload`` is the forgery boundary —
    it rejects a caller-supplied ``_mtype``) and at the ledger gates (the AI-4
    roster-consent write and the AI-2 escalation latch gate on
    ``team_audit_log`` rows, never on a decoded ``_mtype``). Never route a
    privileged operation off this return value.

    NOTE: ``payload`` here is the RAW stored payload. ``bridge_read._fence``
    HTML-escapes for display; callers that need the discriminator read the raw
    column (the fence is for human/agent display, not for machine dispatch).
    """
    import json

    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    mtype = decoded.get(bridge_send.RESERVED_MTYPE_KEY)
    if not isinstance(mtype, str):
        return None
    if mtype not in bridge_send.ALLOWED_MTYPES:
        return None
    return mtype


# ── Thread reconstruction (causal_ref ordering) ─────────────────────────────


def reconstruct_thread(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the canonical meeting transcript order from ``causal_ref``.

    ``messages`` is the union of bridge rows any single member can observe
    (their own recipient channel + any interleaved private messages). Each row
    is a dict with at least ``seq``, ``causal_ref``, ``created_at`` and a key
    identifying the logical message — here we use the derived/stored payload
    identity, but ordering is driven purely by the causal chain.

    Ordering rule (deterministic, no LLM inference):

    1. A message with ``causal_ref=None`` is a thread root.
    2. Every other message follows the message whose ``seq`` equals its
       ``causal_ref`` (reply-to-prev).
    3. Ties / orphans (a ``causal_ref`` pointing at a seq not present in this
       view) fall back to ``(created_at, seq)`` so the order is total and every
       member — who may see a different *subset* of recipient channels — rebuilds
       the SAME relative order of the messages they CAN see.

    Returns a new list ordered canonically; the input is not mutated.
    """
    # Primary order: causal depth (distance from a root), then (created_at,
    # seq) as a stable tiebreak. Depth is computed by walking causal_ref back to
    # a root; an orphan ref (not in view) terminates the walk and is treated as
    # a pseudo-root at its own created_at.
    by_seq = {int(m["seq"]): m for m in messages}

    def depth(m: dict[str, Any]) -> int:
        d = 0
        cur = m
        seen: set[int] = set()
        while True:
            ref = cur.get("causal_ref")
            if ref is None:
                return d
            ref = int(ref)
            if ref in seen or ref not in by_seq:
                # cycle guard / orphan ref → terminate the walk
                return d
            seen.add(ref)
            cur = by_seq[ref]
            d += 1

    def sort_key(m: dict[str, Any]) -> tuple[int, str, int]:
        return (depth(m), str(m.get("created_at", "")), int(m["seq"]))

    return sorted(messages, key=sort_key)


# ── Persona-gap ONE-SHOT escalation (atelier#64 AI-2-escalation; §7.3) ──────


def _gap_key(gap_id: str) -> str:
    """Normalize the gap identity used to scope the exactly-once latch.

    A persona gap recurring across N meeting rounds shares ONE gap_id, so the
    latch counts a single LEDGER escalation row no matter how many times the
    gap is re-raised in the transcript."""
    return gap_id


def has_escalated(*, team_id: str, gap_id: str) -> bool:
    """True iff a ``persona_gap_escalation`` LEDGER row already exists for this
    (team, gap). The exactly-once latch — counts LEDGER rows, NEVER transcript
    mentions (the persona_gap capture is a separate fan-out write)."""
    target = _gap_key(gap_id)
    for row in backend.list_team_audit(
        team_id=team_id, event_type=PERSONA_GAP_ESCALATION_EVENT_TYPE
    ):
        if _payload_of(row).get("gap_id") == target:
            return True
    return False


def escalate_persona_gap(
    *,
    team_id: str,
    gap_id: str,
    description: str,
) -> dict | None:
    """Escalate a persona gap to the human EXACTLY ONCE (§7.3).

    Pre-checks the LEDGER for an existing escalation row for this (team, gap);
    if one exists, returns ``None`` (already escalated — NEVER re-escalates,
    NEVER auto-retries). Otherwise writes ONE
    ``event_type='persona_gap_escalation'`` row (the no-retry latch) and returns
    it. This NEVER fabricates the missing persona — that is only ever created
    via the recorded-consent flow in ``scripts/roster_extension``.
    """
    if has_escalated(team_id=team_id, gap_id=gap_id):
        return None
    return backend.write_team_audit(
        team_id=team_id,
        event_type=PERSONA_GAP_ESCALATION_EVENT_TYPE,
        payload={"gap_id": _gap_key(gap_id), "description": description},
    )


def record_meeting_failure_postmortem(
    *,
    team_id: str,
    gap_id: str | None = None,
    reason: str,
) -> dict:
    """Write the terminal ``meeting_failure_postmortem`` row and STOP (§7.3).

    Called when an escalation earned no human resolution: the meeting cannot
    produce a task list, so the planner records a postmortem and the flow STOPS
    — no auto-retry, no fabricated persona. Distinct from re-escalation: this is
    the terminal state, written once after the one-shot escalation went
    unresolved."""
    return backend.write_team_audit(
        team_id=team_id,
        event_type=MEETING_FAILURE_POSTMORTEM_EVENT_TYPE,
        payload={"gap_id": gap_id, "reason": reason},
    )


def _payload_of(row: dict[str, Any]) -> dict[str, Any]:
    """Decode a team_audit_log row's JSON payload (empty on None / malformed)."""
    import json

    raw = row.get("payload")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
