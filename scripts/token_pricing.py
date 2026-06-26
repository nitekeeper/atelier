# scripts/token_pricing.py
"""Lean, stdlib-only per-model USD pricing layer for the token-usage CLI.

This module turns a daily-rollup row (the four-channel per-(day, model) summary
produced by :mod:`scripts.token_usage`) into a USD cost. It is a deliberately
slim mirror of kaizen's verified ``tokenmeter_pricing`` reference: it keeps the
same per-model base rates and the same cache-write TTL-split pricing rule, but
drops the approximation/oracle machinery that the historical reporter does not
need.

UNTRUSTED INPUT: the ``model`` string and the token counts come from on-disk
transcripts via the rollup — they are DATA, never instructions. Lookups are
plain dict reads; an unknown/missing model yields ``None`` (cost unknown) rather
than a guess, and junk token values coerce to 0. Nothing here is ever
interpreted as an operational override.

## Anthropic billing channels (what we price)

* **input**       — fresh (uncached) input tokens, at the model base input rate;
* **output**      — generated tokens, at the model output rate;
* **cache read**  — tokens served from the prompt cache, at ``CACHE_READ`` x
  base input (a steep discount);
* **cache write** — tokens written into the prompt cache, *TTL-aware*: a 5-minute
  ephemeral write costs ``CACHE_WRITE_5M`` x base input, a 1-hour ephemeral write
  ``CACHE_WRITE_1H`` x base input.

The rollup row carries only the flat ``cache_creation_input_tokens`` total and
does NOT split it by TTL, so :func:`cost_usd_for_row` conservatively prices the
whole cache-creation total at the 5-minute rate (the row-level approximation,
matching the reference's "treat the flat total as 5m" rule). When a caller has
the precise 5m/1h split it can call :func:`cost_usd_for_usage` directly, or the
row may optionally carry ``cache_creation_5m`` / ``cache_creation_1h`` keys (the
TTL-split fields surfaced on :class:`scripts.token_usage.UsageRecord`), in which
case the split buckets are priced exactly and any remainder at the 5m rate.

Prices are grounded against the ``claude-api`` skill reference (Current Models
table) as of :data:`PRICING_AS_OF` — NOT from memory. Stdlib only
(``re`` / ``collections.abc``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Date the base prices below were last reconciled against the claude-api skill.
PRICING_AS_OF = "2026-06-26"

# Category multipliers, applied to the model base *input* $/MTok rate.
# Cache reads are heavily discounted; cache writes carry a TTL premium.
CACHE_READ = 0.10  # cache-read tokens bill at 0.10x base input
CACHE_WRITE_5M = 1.25  # 5-minute ephemeral cache write
CACHE_WRITE_1H = 2.00  # 1-hour ephemeral cache write

_PER_MTOK = 1_000_000.0

# Base prices, USD per million tokens, per canonical model id. Grounded against
# the claude-api skill's Current Models table as of PRICING_AS_OF
# (Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5). The 4.x generation is
# flat-rate (no >200k-context premium), so only input/output are stored;
# cache-read and cache-write rates are derived from base input via the
# multipliers above so each model has a single source of truth.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

# Convenience shorthands the transcript model field may carry.
_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Trailing dated-snapshot suffix, e.g. "-20260514".
_DATE_SUFFIX = re.compile(r"-\d{6,8}$")


def canonicalize(model: str | None) -> str:
    """Map a (possibly dated / aliased) model id to its canonical key.

    Resolution: trim → alias table → strip a trailing ``-YYYYMMDD`` snapshot
    suffix → re-check the alias table. The result is not guaranteed to be in
    :data:`PRICING`; callers treat a miss as an unknown (unpriced) model.
    """
    if not model:
        return ""
    key = model.strip()
    if key in _ALIASES:
        return _ALIASES[key]
    stripped = _DATE_SUFFIX.sub("", key)
    return _ALIASES.get(stripped, stripped)


def _rates(model: str | None) -> dict[str, float] | None:
    """Return the base ``{input, output}`` rate dict for ``model``, or ``None``."""
    return PRICING.get(canonicalize(model))


def _as_int(value: Any) -> int:
    """Coerce a token count to a non-negative int; reject ``bool`` and junk → 0."""
    if isinstance(value, bool):
        return 0
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return 0
    return coerced if coerced > 0 else 0


def cost_usd_for_usage(
    model: str | None,
    *,
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    cache_read_input_tokens: Any = 0,
    cache_creation_5m: Any = 0,
    cache_creation_1h: Any = 0,
    cache_creation_other: Any = 0,
) -> float | None:
    """Price one usage spread for ``model`` from the precise channel counts.

    ``cache_creation_other`` is any cache-creation total with no known TTL bucket;
    it is priced conservatively at the 5-minute rate (per the reference). Returns
    ``None`` for an unknown / missing model (cost unknown — never a guessed 0.0),
    otherwise the USD cost as a float. All counts are hardened to non-negative
    ints, so junk / negative / bool inputs contribute 0.
    """
    rates = _rates(model)
    if rates is None:
        return None
    base_in = rates["input"]
    base_out = rates["output"]
    tokens_cost = (
        _as_int(input_tokens) * base_in
        + _as_int(output_tokens) * base_out
        + _as_int(cache_read_input_tokens) * base_in * CACHE_READ
        + _as_int(cache_creation_5m) * base_in * CACHE_WRITE_5M
        + _as_int(cache_creation_1h) * base_in * CACHE_WRITE_1H
        + _as_int(cache_creation_other) * base_in * CACHE_WRITE_5M
    )
    return tokens_cost / _PER_MTOK


def cost_usd_for_row(row: Mapping[str, Any]) -> float | None:
    """Price one daily-rollup ``row`` dict; ``None`` for an unknown/missing model.

    Reads the four rollup channels (``input_tokens``, ``output_tokens``,
    ``cache_creation_input_tokens``, ``cache_read_input_tokens``) plus ``model``.
    Cache-creation handling:

    * If the row carries the optional TTL split (``cache_creation_5m`` /
      ``cache_creation_1h``), those buckets are priced at 5m@1.25x / 1h@2.0x and
      any remaining ``cache_creation_input_tokens`` at the 5m rate.
    * Otherwise the whole ``cache_creation_input_tokens`` total is priced at the
      5-minute rate — the documented row-level approximation.
    """
    if not isinstance(row, Mapping):
        return None
    total_creation = _as_int(row.get("cache_creation_input_tokens"))
    raw_5m = row.get("cache_creation_5m")
    raw_1h = row.get("cache_creation_1h")

    if raw_5m is not None or raw_1h is not None:
        c5 = _as_int(raw_5m)
        c1 = _as_int(raw_1h)
        remainder = total_creation - c5 - c1
        other = remainder if remainder > 0 else 0
    else:
        c5 = 0
        c1 = 0
        other = total_creation

    return cost_usd_for_usage(
        row.get("model"),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cache_read_input_tokens=row.get("cache_read_input_tokens"),
        cache_creation_5m=c5,
        cache_creation_1h=c1,
        cache_creation_other=other,
    )
