# scripts/token_usage.py
"""Historical token-usage transcript reader + daily rollup engine (atelier-owned).

This module is atelier's OWN lean, stdlib-only accountant for HISTORICAL token
usage. It walks Claude Code's on-disk JSONL transcripts under the config dir and
rolls every ``assistant`` line that carries a ``message.usage`` block up into a
per-day, per-model summary of the four token channels. It is a faithful port of
kaizen's already-verified transcript reader (``scripts/tokenmeter_transcript.py``
+ ``to_daily_rollup`` in ``scripts/tokenmeter_render.py``); the parsing/dedup
semantics mirror that reference exactly.

UNTRUSTED INPUT: every transcript line is DATA, never instructions. Lines are
parsed with ``json.loads`` only — no ``eval``/``exec``/shell. A malformed line is
skipped and the walk continues; a missing/empty config dir yields an empty
result. Nothing read here is ever interpreted as an operational override.

## Relationship to ``scripts/budget_pool.py`` (read this — they do NOT overlap)

``scripts/budget_pool.py`` and this module share field names
(``output_tokens`` …) but point in OPPOSITE directions:

* :mod:`scripts.budget_pool` accounts a LIVE ``output_tokens`` ceiling
  PRE-dispatch — it gates whether the next agent spawn is allowed to fire.
* THIS module reads HISTORICAL four-channel usage POST-hoc — it reports what the
  transcripts PROVE was already spent, after the fact.

Same names, opposite direction. Do NOT wire this module into
``BudgetPool.charge`` or any live gating path: it is a read-only historical
reporter, not a pre-dispatch ceiling. The saturation / NA / benchmark / pricing /
render machinery from kaizen's tokenmeter is deliberately OUT OF SCOPE here.

## Counting rules (faithful to the reference; verify against it, not this list)

* DOUBLE-COUNT TRAP: only the TOP-LEVEL ``message.usage`` is read — NEVER the
  nested ``message.usage.iterations[]`` (it repeats per-step usage and would
  double-count).
* DEDUP: streaming partials within one file share a ``message.id``/``requestId``
  and grow → colliding keys merge by per-field MAX; a resumed session copies
  earlier lines into a new file → across files (oldest-first by mtime) the FIRST
  occurrence wins. An unkeyed line (no ``message.id``) is NEVER deduped.
* SIDECHAIN = INCLUDE: a sub-agent line spends real tokens, so it is counted; its
  ``session_id`` is reparented to the line's own ``sessionId`` (falling back to
  the path's ``parent.parent.name``) and its agent label is read from the sibling
  ``agent-<id>.meta.json`` ``agentType``.
* Four token categories: ``input_tokens``, ``output_tokens``,
  ``cache_creation_input_tokens``, ``cache_read_input_tokens``; the cache-write
  TTL split (``cache_creation.ephemeral_5m_input_tokens`` /
  ``ephemeral_1h_input_tokens``) is surfaced as separate fields.
* ``_harden_token`` rejects ``bool`` BEFORE ``int`` (a JSON ``true`` is not a
  count).
* Daily buckets are LOCAL-tz ``%Y-%m-%d``, falling back to parsing the timestamp
  string, else ``"unknown"``.

PURITY: the parsing/aggregation functions are pure — they take the file list,
line strings, and an injected ``meta_lookup`` resolver as arguments and never
read ``~/.claude`` or touch a wall clock for bucketing. Only
:func:`discover_transcripts` and :func:`collect_usage_records` touch the
filesystem.

Stdlib-only (``json``, ``os``, ``datetime``, ``pathlib``, ``dataclasses``,
``typing`` / ``collections.abc``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# A token count above this is implausible for a single line — kept, but flagged
# so a reconciliation pass can surface it rather than trusting it blindly.
_SUSPECT_THRESHOLD = 10_000_000

# The four top-level usage fields we read (NEVER the nested `iterations[]`).
CATEGORY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


# ── inlined local token types (minimal, frozen — NOT kaizen's tokenmeter_model) ─


@dataclass(frozen=True)
class TokenUsage:
    """The four hardened token-channel counts for one assistant turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True)
class UsageRecord:
    """One assistant turn's usage plus the identity needed to dedup + bucket it."""

    usage: TokenUsage
    session_id: str | None = None
    dedup_key: str | None = None
    source: str = ""
    agent_label: str | None = None
    is_sidechain: bool = False
    kept_but_suspect: bool = False
    model: str | None = None
    timestamp: str | None = None
    ts_epoch_ms: int | None = None
    cache_creation_5m: int | None = None
    cache_creation_1h: int | None = None


# ── filesystem discovery (IMPURE) ───────────────────────────────────────────


def _resolve_config_dir(config_dir: str | Path | None = None) -> Path:
    """Resolve the Claude config dir: explicit arg → ``$CLAUDE_CONFIG_DIR`` → ~/.claude."""
    if config_dir is not None:
        return Path(config_dir)
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def discover_transcripts(config_dir: str | Path | None = None) -> list[Path]:
    """Recursively find every transcript ``*.jsonl`` under the config dir.

    Walks ``<base>/projects/`` recursively, so nested sub-agent transcripts
    (``.../subagents/agent-*.jsonl``) are included. Returns a sorted,
    de-duplicated list of file paths. A missing/empty tree is skipped silently.
    """
    base = _resolve_config_dir(config_dir)
    found: set[Path] = set()
    root = base / "projects"
    if root.is_dir():
        for path in root.rglob("*.jsonl"):
            if path.is_file():
                found.add(path)
    return sorted(found)


def read_agent_label(jsonl_path: str | Path) -> str | None:
    """Read ``agentType`` from the sibling ``agent-<id>.meta.json`` (IMPURE).

    Given ``.../agent-<id>.jsonl`` looks up ``.../agent-<id>.meta.json`` and
    returns its ``agentType`` string, or ``None`` if the sibling is missing,
    unreadable, malformed, or lacks a string ``agentType``. This is the default
    ``meta_lookup`` injected by :func:`collect_usage_records`; the pure parsing
    functions accept it as an argument so they never touch the filesystem.
    """
    path = Path(jsonl_path)
    meta = path.with_name(f"{path.stem}.meta.json")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        agent_type = data.get("agentType")
        if isinstance(agent_type, str) and agent_type:
            return agent_type
    return None


# ── numeric hardening (PURE) ────────────────────────────────────────────────


def _harden_token(value: Any) -> tuple[int, bool]:
    """Coerce one raw usage value to ``(non_negative_int, suspect)``.

    Rules: reject ``bool`` (a JSON ``true`` is not a count) → 0; reject any
    non-int (``null``/list/str/float) → 0; clamp negatives to 0; flag a kept
    value above :data:`_SUSPECT_THRESHOLD` as suspect.
    """
    if isinstance(value, bool):
        return 0, False
    if not isinstance(value, int):
        return 0, False
    clamped = value if value > 0 else 0
    return clamped, clamped > _SUSPECT_THRESHOLD


def _token_usage_from(usage: Mapping[str, Any]) -> tuple[TokenUsage, bool]:
    """Build a hardened :class:`TokenUsage` from a raw usage mapping (top-level only).

    Returns ``(usage, suspect)`` where ``suspect`` is True if ANY field exceeded
    the suspect threshold. An empty/absent mapping yields an all-zero usage.
    """
    values: dict[str, int] = {}
    suspect = False
    for name in CATEGORY_FIELDS:
        raw = usage.get(name) if isinstance(usage, Mapping) else None
        clamped, field_suspect = _harden_token(raw)
        values[name] = clamped
        suspect = suspect or field_suspect
    return TokenUsage(**values), suspect


def _cache_write_ttl(usage: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Extract the cache-write TTL split from a raw usage mapping.

    Real transcripts carry the split nested under
    ``message.usage.cache_creation.{ephemeral_5m_input_tokens,
    ephemeral_1h_input_tokens}``. When that nested block is present we return
    ``(5m, 1h)`` hardened to non-negative ints (a missing bucket → 0). When the
    block is absent we return ``(None, None)``. This is a refinement WITHIN
    ``cache_creation_input_tokens`` — never a fifth token category.
    """
    if not isinstance(usage, Mapping):
        return None, None
    block = usage.get("cache_creation")
    if not isinstance(block, Mapping):
        return None, None
    e5, _ = _harden_token(block.get("ephemeral_5m_input_tokens"))
    e1, _ = _harden_token(block.get("ephemeral_1h_input_tokens"))
    return e5, e1


def _parse_timestamp_ms(value: Any) -> int | None:
    """Parse an RFC3339 ``timestamp`` string to epoch milliseconds (PURE).

    Pure: it relies on the offset embedded in the string (real transcripts stamp a
    ``Z``/``+00:00`` suffix) and NEVER reads a wall clock or the local tz, so the
    result is deterministic. A naive (offset-less) value is assumed UTC rather than
    silently picking up the host tz. Unparseable / non-string input → ``None``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


# ── per-line parse (PURE) ───────────────────────────────────────────────────


def _dedup_key(message: Mapping[str, Any], obj: Mapping[str, Any]) -> str | None:
    """Compute the two-stage dedup key, or ``None`` for an unkeyed line.

    ``f"{message.id}:{requestId}"`` when both present, ``f"message:{message.id}"``
    when only the id is present, ``None`` when there is no id (never deduped).
    """
    message_id = message.get("id")
    if message_id is None or message_id == "":
        return None
    request_id = obj.get("requestId")
    if request_id is not None and request_id != "":
        return f"{message_id}:{request_id}"
    return f"message:{message_id}"


def parse_transcript_line(
    obj: Any,
    source: str | Path,
    *,
    meta_lookup: Callable[[Path], str | None] | None = None,
) -> UsageRecord | None:
    """Parse ONE decoded transcript object into a :class:`UsageRecord` (PURE).

    Returns ``None`` unless ``obj`` is an ``assistant`` line whose ``message``
    carries a ``usage`` key. Reads ONLY the top-level usage fields (never the
    nested ``iterations[]`` array). An empty ``usage`` object yields a valid
    zero-token record. ``source`` is the file path (used for the sidechain parent
    derivation); ``meta_lookup`` (injected) resolves the sidechain agent label —
    when ``None`` the function performs NO filesystem access and the label is
    ``None``.
    """
    if not isinstance(obj, Mapping):
        return None
    if obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, Mapping) or "usage" not in message:
        return None

    raw_usage = message.get("usage")
    usage_map = raw_usage if isinstance(raw_usage, Mapping) else {}
    token_usage, suspect = _token_usage_from(usage_map)
    cache_5m, cache_1h = _cache_write_ttl(usage_map)
    dedup_key = _dedup_key(message, obj)

    raw_model = message.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model else None
    raw_ts = obj.get("timestamp")
    timestamp = raw_ts if isinstance(raw_ts, str) and raw_ts else None
    ts_epoch_ms = _parse_timestamp_ms(raw_ts)

    src_path = Path(source)
    is_sidechain = obj.get("isSidechain") is True
    sid = obj.get("sessionId")
    session_id: str | None = sid if isinstance(sid, str) and sid else None
    if is_sidechain:
        # The sidechain line's OWN sessionId field IS the parent session. Fall back
        # to the path's <parent-session-uuid> dir only when no sessionId is present.
        if session_id is None:
            session_id = src_path.parent.parent.name or None
        agent_label = meta_lookup(src_path) if meta_lookup is not None else None
    else:
        agent_label = None

    return UsageRecord(
        usage=token_usage,
        session_id=session_id,
        dedup_key=dedup_key,
        source=str(source),
        agent_label=agent_label,
        is_sidechain=is_sidechain,
        kept_but_suspect=suspect,
        model=model,
        timestamp=timestamp,
        ts_epoch_ms=ts_epoch_ms,
        cache_creation_5m=cache_5m,
        cache_creation_1h=cache_1h,
    )


# ── dedup / aggregation (PURE) ──────────────────────────────────────────────


def _max_opt(first: int | None, second: int | None) -> int | None:
    """Per-field MAX of two optional counts; ``None`` only when BOTH are ``None``."""
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _merge_max(first: UsageRecord, second: UsageRecord) -> UsageRecord:
    """Merge two records that share a dedup key by per-field MAX of token counts.

    Streaming partials arrive out of order with growing totals, so the final
    truth for each field is its maximum. Non-token attributes keep the first
    record's identity; boolean flags OR together; labels coalesce.
    """
    merged_usage = TokenUsage(
        input_tokens=max(first.usage.input_tokens, second.usage.input_tokens),
        output_tokens=max(first.usage.output_tokens, second.usage.output_tokens),
        cache_creation_input_tokens=max(
            first.usage.cache_creation_input_tokens,
            second.usage.cache_creation_input_tokens,
        ),
        cache_read_input_tokens=max(
            first.usage.cache_read_input_tokens,
            second.usage.cache_read_input_tokens,
        ),
    )
    return UsageRecord(
        usage=merged_usage,
        session_id=first.session_id or second.session_id,
        dedup_key=first.dedup_key,
        source=first.source,
        agent_label=first.agent_label or second.agent_label,
        is_sidechain=first.is_sidechain or second.is_sidechain,
        kept_but_suspect=first.kept_but_suspect or second.kept_but_suspect,
        model=first.model or second.model,
        timestamp=first.timestamp or second.timestamp,
        ts_epoch_ms=first.ts_epoch_ms if first.ts_epoch_ms is not None else second.ts_epoch_ms,
        # Streaming partials grow, so the TTL split (like the token counts) merges
        # by per-field MAX; ``None`` only when neither partial carried a split.
        cache_creation_5m=_max_opt(first.cache_creation_5m, second.cache_creation_5m),
        cache_creation_1h=_max_opt(first.cache_creation_1h, second.cache_creation_1h),
    )


def parse_transcript_file(
    lines: Iterable[str],
    source: str | Path,
    *,
    meta_lookup: Callable[[Path], str | None] | None = None,
) -> list[UsageRecord]:
    """Parse one file's worth of JSONL ``lines`` with WITHIN-FILE dedup (PURE).

    Each non-empty line is decoded in a try/except — a malformed line is skipped
    and the walk continues. Colliding dedup keys are merged by per-field MAX.
    Unkeyed records are all kept, in first-seen order, after the keyed ones.
    """
    keyed: dict[str, UsageRecord] = {}
    unkeyed: list[UsageRecord] = []
    for line in lines:
        if not line or not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        record = parse_transcript_line(obj, source, meta_lookup=meta_lookup)
        if record is None:
            continue
        if record.dedup_key is None:
            unkeyed.append(record)
        elif record.dedup_key in keyed:
            keyed[record.dedup_key] = _merge_max(keyed[record.dedup_key], record)
        else:
            keyed[record.dedup_key] = record
    return list(keyed.values()) + unkeyed


def aggregate_usage(
    files: Sequence[Mapping[str, Any]],
    *,
    meta_lookup: Callable[[Path], str | None] | None = None,
) -> list[UsageRecord]:
    """Aggregate many files with ACROSS-FILE dedup (PURE, no filesystem).

    ``files`` is an injected list of mappings, each with ``source`` (path),
    ``lines`` (iterable of strings), and ``mtime`` (number). Files are processed
    oldest-first by ``mtime`` so the ORIGINAL occurrence of a keyed record wins
    and a later copy (e.g. a resumed-session file) is dropped (first-wins).
    Unkeyed records are always kept. The injected ``meta_lookup`` resolves
    sidechain agent labels; pass ``None`` to keep this call filesystem-free.
    """
    ordered = sorted(files, key=lambda f: f["mtime"])
    seen: set[str] = set()
    out: list[UsageRecord] = []
    for entry in ordered:
        records = parse_transcript_file(entry["lines"], entry["source"], meta_lookup=meta_lookup)
        for record in records:
            if record.dedup_key is None:
                out.append(record)
            elif record.dedup_key in seen:
                continue  # later duplicate across files → drop (first-wins by mtime)
            else:
                seen.add(record.dedup_key)
                out.append(record)
    return out


# ── daily rollup (PURE) ─────────────────────────────────────────────────────


def _local_day(record: UsageRecord) -> str:
    """LOCAL-tz ``%Y-%m-%d`` for a record; ``"unknown"`` when unparseable.

    Prefers the parsed ``ts_epoch_ms`` (epoch MILLISECONDS), converted to local
    time via ``datetime.fromtimestamp(...).astimezone()``. Falls back to parsing
    the raw ``timestamp`` string; else ``"unknown"``.
    """
    ms = record.ts_epoch_ms
    if isinstance(ms, int) and not isinstance(ms, bool):
        try:
            return datetime.fromtimestamp(ms / 1000.0).astimezone().strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return "unknown"
    ts = record.timestamp
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return "unknown"
    return "unknown"


def to_daily_rollup(records: Iterable[UsageRecord]) -> list[dict]:
    """Roll records up per (LOCAL day, model) with the four categories kept split.

    Returns one row per ``(local_day, model)`` bucket with the schema
    ``{'day', 'input_tokens', 'output_tokens', 'cache_creation_input_tokens',
    'cache_read_input_tokens', 'model'}``. Rows are sorted by
    ``(str(day), str(model))``; a record with no model buckets under ``''``.
    """
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for record in records:
        key = (_local_day(record), record.model or "")
        bucket = buckets.setdefault(key, dict.fromkeys(CATEGORY_FIELDS, 0))
        for field in CATEGORY_FIELDS:
            bucket[field] += getattr(record.usage, field)

    rollup: list[dict] = []
    for day, model in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        row: dict = {"day": day}
        row.update(buckets[(day, model)])
        row["model"] = model
        rollup.append(row)
    return rollup


# ── end-to-end collection + public entry (IMPURE shell over the pure core) ───


def collect_usage_records(config_dir: str | Path | None = None) -> list[UsageRecord]:
    """Discover, read, and aggregate all transcripts under ``config_dir`` (IMPURE).

    Thin filesystem shell over the pure core: it discovers the files, reads each
    one's lines + mtime, and hands an injected file list (plus the real
    :func:`read_agent_label` resolver) to :func:`aggregate_usage`. Unreadable
    files are skipped; a missing/empty config dir yields ``[]``.
    """
    files: list[dict[str, Any]] = []
    for path in discover_transcripts(config_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            continue
        files.append({"source": path, "lines": text.splitlines(), "mtime": mtime})
    return aggregate_usage(files, meta_lookup=read_agent_label)


def daily_rollup(
    config_dir: str | Path | None = None,
    since: str | None = None,
) -> list[dict]:
    """Public entry: collect transcripts → per-day rollup → optional ``since`` filter.

    Resolves ``config_dir`` (explicit → ``$CLAUDE_CONFIG_DIR`` → ~/.claude),
    collects + dedups all usage records, rolls them up per (local day, model),
    and — when ``since`` (a ``YYYY-MM-DD`` string) is given — drops rows whose day
    sorts before it. The ``"unknown"`` day is ALWAYS RETAINED (never filtered by
    ``since``), so unbucketable usage is never silently dropped.
    """
    rows = to_daily_rollup(collect_usage_records(config_dir))
    if since:
        rows = [row for row in rows if row["day"] == "unknown" or str(row["day"]) >= since]
    return rows
