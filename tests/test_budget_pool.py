"""Tests for scripts/budget_pool.py.

Coverage
--------
* Headroom math — effective_ceiling = floor(total * headroom)
* assert_can_dispatch boundaries:
    - spent + est == ceiling  → ALLOWED (boundary case)
    - spent + est >  ceiling  → RAISES BudgetExceeded
* charge() accumulates spent
* charge() bubbles to parent pool
* BudgetExceeded carries correct spent/est/ceiling attributes
* static_fleet_width: only ever narrows max_workers, never widens
* static_fleet_width: returns 0 when budget is exhausted
* remaining() never goes below 0
* Invalid construction raises ValueError
* usage_breakdown side counters accumulate + bubble; gate stays output-only
"""

from __future__ import annotations

import pytest

from scripts.budget_pool import BudgetExceeded, BudgetPool

# ── headroom math ──────────────────────────────────────────────────────────


def test_effective_ceiling_default_headroom():
    pool = BudgetPool(total_tokens=1000)
    assert pool.effective_ceiling == 700  # floor(1000 * 0.70)


def test_effective_ceiling_custom_headroom():
    pool = BudgetPool(total_tokens=1000, headroom=0.5)
    assert pool.effective_ceiling == 500


def test_effective_ceiling_full_headroom():
    """headroom=1.0 means the full total is the ceiling."""
    pool = BudgetPool(total_tokens=500, headroom=1.0)
    assert pool.effective_ceiling == 500


def test_remaining_starts_at_ceiling():
    pool = BudgetPool(total_tokens=1000)
    assert pool.remaining() == pool.effective_ceiling


def test_remaining_never_negative():
    """remaining() is clamped to 0 even after overcharge (defensive)."""
    pool = BudgetPool(total_tokens=100)
    # Manually inflate spent past ceiling — charge doesn't guard, but remaining() clamps.
    pool.charge({"output_tokens": 1000})
    assert pool.remaining() == 0


# ── charge + spent ─────────────────────────────────────────────────────────


def test_charge_accumulates_spent():
    pool = BudgetPool(total_tokens=1000)
    pool.charge({"output_tokens": 100})
    pool.charge({"output_tokens": 250})
    assert pool.spent() == 350


def test_charge_missing_key_treated_as_zero():
    """Missing output_tokens key is treated as 0, not an error."""
    pool = BudgetPool(total_tokens=1000)
    pool.charge({})
    assert pool.spent() == 0


def test_charge_extra_keys_ignored():
    """Extra usage keys (e.g. input_tokens) don't affect spent."""
    pool = BudgetPool(total_tokens=1000)
    pool.charge({"input_tokens": 999, "output_tokens": 50, "total_cost_usd": 0.01})
    assert pool.spent() == 50


# ── parent bubbling ────────────────────────────────────────────────────────


def test_charge_bubbles_to_parent():
    parent = BudgetPool(total_tokens=10_000)
    child = BudgetPool(total_tokens=1_000, parent=parent)
    child.charge({"output_tokens": 200})
    assert child.spent() == 200
    assert parent.spent() == 200, "parent spent must reflect child charge"


def test_charge_does_not_bubble_upward_twice():
    """Charging the parent directly does NOT also charge the grandparent
    via a double-bubble — each charge event propagates exactly one level up."""
    grandparent = BudgetPool(total_tokens=100_000)
    parent = BudgetPool(total_tokens=10_000, parent=grandparent)
    child = BudgetPool(total_tokens=1_000, parent=parent)

    child.charge({"output_tokens": 100})

    assert child.spent() == 100
    assert parent.spent() == 100
    assert grandparent.spent() == 100


def test_parent_ceiling_can_be_hit_by_child_charges():
    """Child charges can exhaust the parent's budget — assert_can_dispatch
    on the parent then raises BudgetExceeded."""
    parent = BudgetPool(total_tokens=1000, headroom=1.0)
    child = BudgetPool(total_tokens=5000, parent=parent)
    child.charge({"output_tokens": 1000})  # fills parent
    with pytest.raises(BudgetExceeded):
        parent.assert_can_dispatch(est_per_agent=1)


# ── usage_breakdown side counters (MAJOR-2) ───────────────────────────────


def test_usage_breakdown_starts_at_zero():
    pool = BudgetPool(total_tokens=1000)
    assert pool.usage_breakdown() == {
        "output_tokens": 0,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_usage_breakdown_accumulates_all_channels():
    """charge() accumulates input + cache channels into side counters,
    output_tokens into the gated counter."""
    pool = BudgetPool(total_tokens=100_000, headroom=1.0)
    pool.charge(
        {
            "output_tokens": 50,
            "input_tokens": 200,
            "cache_creation_input_tokens": 36_000,
            "cache_read_input_tokens": 1_000,
        }
    )
    pool.charge(
        {
            "output_tokens": 25,
            "input_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 500,
        }
    )
    breakdown = pool.usage_breakdown()
    assert breakdown == {
        "output_tokens": 75,
        "input_tokens": 300,
        "cache_creation_input_tokens": 36_000,
        "cache_read_input_tokens": 1_500,
    }
    # The gated counter (spent) equals output_tokens only.
    assert pool.spent() == 75
    assert breakdown["output_tokens"] == pool.spent()


def test_usage_breakdown_bubbles_to_parent():
    """Side counters bubble to the parent exactly like output_tokens."""
    parent = BudgetPool(total_tokens=1_000_000, headroom=1.0)
    child = BudgetPool(total_tokens=100_000, parent=parent)
    child.charge(
        {
            "output_tokens": 40,
            "input_tokens": 500,
            "cache_creation_input_tokens": 12_000,
            "cache_read_input_tokens": 3_000,
        }
    )
    expected = {
        "output_tokens": 40,
        "input_tokens": 500,
        "cache_creation_input_tokens": 12_000,
        "cache_read_input_tokens": 3_000,
    }
    assert child.usage_breakdown() == expected
    assert parent.usage_breakdown() == expected, "side counters must bubble to parent"


def test_gate_ignores_side_channels():
    """assert_can_dispatch is driven ONLY by output_tokens — huge input/cache
    counts must NOT trip the gate."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)  # ceiling = 1000 output tokens
    pool.charge(
        {
            "output_tokens": 100,
            "input_tokens": 999_999,
            "cache_creation_input_tokens": 999_999,
            "cache_read_input_tokens": 999_999,
        }
    )
    # spent (output) is only 100; 100 + 800 <= 1000 → must NOT raise despite
    # the millions of input/cache tokens accumulated.
    pool.assert_can_dispatch(est_per_agent=800)
    assert pool.spent() == 100


# ── assert_can_dispatch boundaries ────────────────────────────────────────


def test_assert_can_dispatch_exactly_at_ceiling_is_allowed():
    """spent + est == ceiling is the ALLOWED boundary (not a breach)."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)  # ceiling = 1000
    pool.charge({"output_tokens": 600})
    # 600 + 400 == 1000 == ceiling → must NOT raise
    pool.assert_can_dispatch(est_per_agent=400)


def test_assert_can_dispatch_one_over_ceiling_raises():
    """spent + est > ceiling by 1 token must raise BudgetExceeded."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)  # ceiling = 1000
    pool.charge({"output_tokens": 600})
    # 600 + 401 == 1001 > 1000 → must raise
    with pytest.raises(BudgetExceeded) as exc_info:
        pool.assert_can_dispatch(est_per_agent=401)
    exc = exc_info.value
    assert exc.spent == 600
    assert exc.est == 401
    assert exc.ceiling == 1000


def test_assert_can_dispatch_zero_spent():
    pool = BudgetPool(total_tokens=1000)
    pool.assert_can_dispatch(est_per_agent=500)  # 0 + 500 <= 700 → ok


def test_assert_can_dispatch_raises_when_already_at_ceiling():
    pool = BudgetPool(total_tokens=1000, headroom=1.0)
    pool.charge({"output_tokens": 1000})
    with pytest.raises(BudgetExceeded):
        pool.assert_can_dispatch(est_per_agent=1)


def test_budget_exceeded_is_terminal_not_re_queueable():
    """BudgetExceeded is a RuntimeError subclass — callers should abort,
    not re-queue."""
    with pytest.raises(RuntimeError):
        raise BudgetExceeded(spent=700, est=100, ceiling=700)


# ── static_fleet_width ────────────────────────────────────────────────────


def test_static_fleet_width_narrows_max_workers():
    """When budget allows fewer than max_workers, the budget wins."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)  # remaining = 1000
    # remaining=1000, per_agent=300 → budget_width=3; max_workers=5 → result=3
    assert BudgetPool.static_fleet_width(pool, per_agent_tokens=300, max_workers=5) == 3


def test_static_fleet_width_never_widens():
    """When budget would allow more than max_workers, max_workers is returned."""
    pool = BudgetPool(total_tokens=100_000, headroom=1.0)  # remaining = 100000
    # budget_width = 100000//100 = 1000; max_workers=5 → result=5 (not 1000)
    assert BudgetPool.static_fleet_width(pool, per_agent_tokens=100, max_workers=5) == 5


def test_static_fleet_width_zero_when_exhausted():
    """Returns 0 when the budget is exhausted (remaining < per_agent_tokens)."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)
    pool.charge({"output_tokens": 950})  # 50 remaining
    # 50 // 100 = 0
    assert BudgetPool.static_fleet_width(pool, per_agent_tokens=100, max_workers=5) == 0


def test_static_fleet_width_exact_divisibility():
    """remaining() exactly divisible by per_agent_tokens returns expected count."""
    pool = BudgetPool(total_tokens=1000, headroom=1.0)
    pool.charge({"output_tokens": 400})  # 600 remaining
    # 600 // 200 = 3
    assert BudgetPool.static_fleet_width(pool, per_agent_tokens=200, max_workers=10) == 3


def test_static_fleet_width_invalid_per_agent_tokens_raises():
    pool = BudgetPool(total_tokens=1000)
    with pytest.raises(ValueError):
        BudgetPool.static_fleet_width(pool, per_agent_tokens=0, max_workers=5)


def test_static_fleet_width_invalid_max_workers_raises():
    pool = BudgetPool(total_tokens=1000)
    with pytest.raises(ValueError):
        BudgetPool.static_fleet_width(pool, per_agent_tokens=100, max_workers=0)


# ── invalid construction ───────────────────────────────────────────────────


def test_invalid_headroom_zero_raises():
    with pytest.raises(ValueError):
        BudgetPool(total_tokens=1000, headroom=0.0)


def test_invalid_headroom_above_one_raises():
    with pytest.raises(ValueError):
        BudgetPool(total_tokens=1000, headroom=1.01)


def test_invalid_total_tokens_zero_raises():
    with pytest.raises(ValueError):
        BudgetPool(total_tokens=0)


def test_invalid_total_tokens_negative_raises():
    with pytest.raises(ValueError):
        BudgetPool(total_tokens=-100)
