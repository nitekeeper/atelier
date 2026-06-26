# tests/test_token_pricing.py
"""Exact-math tests for ``scripts/token_pricing.py``.

Every cost below is computed by hand from the published per-MTok rates
(claude-api skill: Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5) and the
category multipliers (cache-read 0.10x, cache-write 5m 1.25x / 1h 2.0x of base
input). Counts are chosen as whole millions so the results are exact binary
fractions and can be asserted with ``==``.

Stdlib + pytest only.
"""

from __future__ import annotations

from scripts.token_pricing import (
    CACHE_READ,
    CACHE_WRITE_1H,
    CACHE_WRITE_5M,
    canonicalize,
    cost_usd_for_row,
    cost_usd_for_usage,
)

_M = 1_000_000  # one million tokens — makes per-MTok math exact


# ── multipliers are the reference values ────────────────────────────────────


def test_multiplier_constants_match_reference():
    assert CACHE_READ == 0.10
    assert CACHE_WRITE_5M == 1.25
    assert CACHE_WRITE_1H == 2.00


# ── base input / output rates, per model ────────────────────────────────────


def test_opus_base_input_and_output_rates():
    # 1M input @ $5/MTok = $5.00; 1M output @ $25/MTok = $25.00.
    assert cost_usd_for_usage("claude-opus-4-8", input_tokens=_M) == 5.0
    assert cost_usd_for_usage("claude-opus-4-8", output_tokens=_M) == 25.0


def test_sonnet_and_haiku_rates():
    assert cost_usd_for_usage("claude-sonnet-4-6", input_tokens=_M) == 3.0
    assert cost_usd_for_usage("claude-sonnet-4-6", output_tokens=_M) == 15.0
    assert cost_usd_for_usage("claude-haiku-4-5", input_tokens=_M) == 1.0
    assert cost_usd_for_usage("claude-haiku-4-5", output_tokens=_M) == 5.0


# ── cache-read discount + cache-write TTL premiums ──────────────────────────


def test_cache_read_is_tenth_of_base_input():
    # 1M cache-read @ opus = 5.0 * 0.10 = $0.50.
    assert cost_usd_for_usage("claude-opus-4-8", cache_read_input_tokens=_M) == 0.5


def test_cache_write_5m_and_1h_multipliers():
    # 5m: 5.0 * 1.25 = $6.25 ; 1h: 5.0 * 2.0 = $10.00 (per 1M, opus).
    assert cost_usd_for_usage("claude-opus-4-8", cache_creation_5m=_M) == 6.25
    assert cost_usd_for_usage("claude-opus-4-8", cache_creation_1h=_M) == 10.0


def test_cache_creation_other_is_priced_at_5m_rate():
    # The "no TTL bucket known" remainder bills at the conservative 5m rate.
    assert cost_usd_for_usage("claude-opus-4-8", cache_creation_other=_M) == 6.25


# ── unknown / missing model → None (never a guessed 0.0) ────────────────────


def test_unknown_model_returns_none():
    assert cost_usd_for_usage("gpt-4o", input_tokens=_M) is None
    assert cost_usd_for_usage(None, input_tokens=_M) is None
    assert cost_usd_for_usage("", input_tokens=_M) is None


def test_unknown_model_row_returns_none():
    row = {"model": "mystery-model", "input_tokens": _M, "output_tokens": _M}
    assert cost_usd_for_row(row) is None


# ── row-level pricing: flat cache_creation treated as 5m (the approximation) ──


def test_row_flat_cache_creation_priced_as_5m():
    row = {
        "model": "claude-opus-4-8",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": _M,
        "cache_read_input_tokens": 0,
    }
    # Whole flat total at the 5m rate: 5.0 * 1.25 = $6.25.
    assert cost_usd_for_row(row) == 6.25


def test_row_full_four_channel_cost():
    row = {
        "model": "claude-opus-4-8",
        "input_tokens": _M,  # 5.00
        "output_tokens": _M,  # 25.00
        "cache_creation_input_tokens": _M,  # 6.25 (5m approx)
        "cache_read_input_tokens": _M,  # 0.50
    }
    assert cost_usd_for_row(row) == 5.0 + 25.0 + 6.25 + 0.5


# ── row-level pricing: explicit TTL split is priced exactly, remainder @ 5m ──


def test_row_with_exact_ttl_split():
    row = {
        "model": "claude-opus-4-8",
        "cache_creation_input_tokens": _M,
        "cache_creation_5m": 400_000,
        "cache_creation_1h": 600_000,
    }
    # 0.4M @ 6.25 + 0.6M @ 10.0 = 2.5 + 6.0 = $8.50 ; no remainder.
    assert cost_usd_for_row(row) == 8.5


def test_row_split_with_remainder_priced_at_5m():
    row = {
        "model": "claude-opus-4-8",
        "cache_creation_input_tokens": _M,
        "cache_creation_5m": 200_000,
        "cache_creation_1h": 300_000,
    }
    # 0.2M @ 6.25 (1.25) + 0.3M @ 10.0 (3.0) + 0.5M remainder @ 6.25 (3.125) = 7.375.
    assert cost_usd_for_row(row) == 7.375


# ── hardening: bool / junk counts coerce to 0, not int(True) ─────────────────


def test_bool_and_junk_counts_coerce_to_zero():
    assert cost_usd_for_usage("claude-opus-4-8", input_tokens=True) == 0.0
    assert cost_usd_for_usage("claude-opus-4-8", input_tokens=None) == 0.0
    assert cost_usd_for_usage("claude-opus-4-8", input_tokens=-50) == 0.0
    assert cost_usd_for_usage("claude-opus-4-8", input_tokens="garbage") == 0.0


def test_non_mapping_row_returns_none():
    assert cost_usd_for_row(None) is None  # type: ignore[arg-type]


# ── canonicalization: dated snapshots + shorthand aliases ───────────────────


def test_canonicalize_strips_date_suffix_and_resolves_aliases():
    assert canonicalize("claude-opus-4-8-20260514") == "claude-opus-4-8"
    assert canonicalize("opus") == "claude-opus-4-8"
    assert canonicalize("sonnet") == "claude-sonnet-4-6"
    assert canonicalize("haiku") == "claude-haiku-4-5"
    assert canonicalize(None) == ""


def test_dated_snapshot_model_is_priced():
    assert cost_usd_for_usage("claude-opus-4-8-20260514", input_tokens=_M) == 5.0
