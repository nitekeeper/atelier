"""Unit tests for the per-task model-tier policy (scripts/model_tier.py).

Drives the REAL `recommend()` end to end. Covers the resolution precedence
(override → env pin → difficulty → phase → default), the ROLE_FLOOR
RAISES-only invariant, phase-key normalization + case-insensitivity, and the
two LOAD-BEARING cost guarantees as EXACT-MECHANISM assertions (a silent revert
of either is caught):

  * the role floor can only RAISE — a high base with a low floor stays high;
  * the default is SONNET, not opus — a signal-free task must NOT spawn Opus.
"""

from __future__ import annotations

import pytest

from scripts import model_tier
from scripts.model_tier import (
    DEFAULT_TIER,
    DIFFICULTY_TIER,
    ENV_TIER_VAR,
    PHASE_TIER,
    ROLE_FLOOR,
    TIERS,
    _more_capable,
    normalize_phase,
    recommend,
)

# ── Tier vocabulary / invariants ────────────────────────────────────────────


def test_tiers_are_the_three_aliases_in_rank_order():
    assert TIERS == ("haiku", "sonnet", "opus")


def test_default_tier_is_sonnet_not_opus():
    """LOAD-BEARING: the cost guarantee lives in the constant too — the safe
    middle default is sonnet, never opus."""
    assert DEFAULT_TIER == "sonnet"


# ── PHASE_TIER: each phase → expected tier ──────────────────────────────────


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        # opus — reasoning / judgement / high-stakes
        ("design", "opus"),
        ("plan", "opus"),
        ("security", "opus"),
        ("review", "opus"),
        ("handoff", "opus"),
        ("diagnose", "opus"),
        ("tdd:red", "opus"),
        ("abandonment", "opus"),
        ("no-consensus", "opus"),
        # sonnet — medium implementation / verification
        ("tdd", "sonnet"),
        ("tdd:green", "sonnet"),
        ("qa", "sonnet"),
        ("verify", "sonnet"),
        ("receive-review", "sonnet"),
        # haiku — mechanical
        ("doc", "haiku"),
        ("agenda", "haiku"),
        ("status", "haiku"),
        ("format", "haiku"),
    ],
)
def test_recommend_phase_maps_to_expected_tier(phase, expected):
    assert recommend(phase=phase) == expected


def test_recommend_is_self_consistent_with_phase_tier_table():
    """SELF-CONSISTENCY ONLY (not a source-of-truth guard): feeding every
    PHASE_TIER key back through recommend() must return that key's own tier.
    This derives its expectation FROM the table it checks, so it cannot catch a
    table value that diverges from the documented intent — that is the job of the
    HARDCODED parametrized `test_recommend_phase_maps_to_expected_tier` above
    (the real source-of-truth guard) and `test_recommend_real_production_phase_ids`
    below. This one only proves recommend() round-trips its own bare keys."""
    for phase, tier in PHASE_TIER.items():
        assert recommend(phase=phase) == tier


# ── Phase-key normalization + case-insensitivity ────────────────────────────


def test_phase_key_normalization_variants_resolve_sensibly():
    # tdd / tdd:green / tdd-green all resolve to the medium tier.
    assert recommend(phase="tdd") == "sonnet"
    assert recommend(phase="tdd:green") == "sonnet"
    assert recommend(phase="tdd-green") == "sonnet"
    # tdd:red is the test-DESIGN opus phase (compound key wins over the base).
    assert recommend(phase="tdd:red") == "opus"
    assert recommend(phase="tdd-red") == "opus"


def test_phase_with_state_suffix_falls_to_base():
    # A `base:state` token (design:open, tdd:in-progress) resolves to the base.
    assert recommend(phase="design:open") == "opus"
    assert recommend(phase="tdd:in-progress") == "sonnet"
    assert recommend(phase="review:active") == "opus"


def test_phase_is_case_insensitive():
    assert recommend(phase="DESIGN") == "opus"
    assert recommend(phase="  Review  ") == "opus"
    assert recommend(phase="Doc") == "haiku"
    assert recommend(phase="TDD:GREEN") == "sonnet"


def test_normalize_phase_direct():
    assert normalize_phase("TDD:green") == "tdd:green"
    assert normalize_phase("tdd-green") == "tdd:green"
    assert normalize_phase("design:open") == "design"
    assert normalize_phase("  ") is None
    assert normalize_phase(None) is None


# ── PRODUCTION-CALLER coverage — the REAL <base>:<state> phase ids ──────────
#
# The tests above only fed BARE keys ("review", "tdd") — but production callers
# pass the atelier `phases` table id `<base>:<state>` as returned by `get_phase`
# (e.g. "review:approved", "tdd:red"), OR the `dev:<base>` phase-GROUP form
# ("dev:review"). This proves recommend() resolves the REAL strings, so a
# normalize_phase regression that broke the production format would FAIL here
# (the bare-key tests would not). The list is byte-faithful to the `phases`
# table ids documented against migrations/shared/001_v110_schema.sql.


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        # ── opus: reasoning / judgement / high-stakes (real <base>:<state>) ──
        ("design:open", "opus"),
        ("plan:approved", "opus"),
        ("review:open", "opus"),
        ("review:approved", "opus"),
        ("review:changes-requested", "opus"),  # multi-word state must NOT corrupt the base
        ("security:open", "opus"),
        ("security:approved", "opus"),
        ("tdd:red", "opus"),  # compound key wins (test-DESIGN is judgement)
        ("diagnose:open", "opus"),
        ("diagnose:resolved", "opus"),
        ("handoff:complete", "opus"),
        ("no-consensus:reached", "opus"),  # hyphenated KEY must survive the colon walk
        # ── the dev:<base> phase-GROUP form (the namespace-prefix path) ──
        ("dev:review", "opus"),
        ("dev:tdd:red", "opus"),  # dev: prefix stripped → tdd:red compound key
        # ── sonnet: medium implementation / verification ──
        ("tdd:green", "sonnet"),
        ("tdd:clean", "sonnet"),
        ("qa:open", "sonnet"),
        ("qa:approved", "sonnet"),
        ("verify:open", "sonnet"),
        ("receive-review:open", "sonnet"),  # hyphenated KEY + a :state suffix
        ("dev:tdd", "sonnet"),  # dev: prefix stripped → tdd base
        # ── haiku: mechanical ──
        ("doc:open", "haiku"),
        ("agenda", "haiku"),
        ("status", "haiku"),
    ],
)
def test_recommend_real_production_phase_ids(phase, expected):
    """recommend() drives the REAL production phase strings (the `phases` table
    `<base>:<state>` ids + the `dev:<base>` group form) to the right tier.

    This is the production-caller coverage the bare-key tests miss: a
    normalize_phase change that mishandled the `<base>:<state>` format (e.g. an
    unconditional `-`→`:` rewrite splitting `no-consensus`) would turn these RED
    while the bare-key tests stayed green."""
    assert recommend(phase=phase) == expected


# ── Unknown phase → default (no crash) ──────────────────────────────────────


def test_unknown_phase_returns_default_sonnet():
    assert recommend(phase="totally-made-up-phase") == "sonnet"


def test_no_signal_at_all_returns_default_sonnet():
    """LOAD-BEARING cost guarantee: a plain task with NO signal must NOT spawn
    Opus — it gets the sonnet middle default."""
    assert recommend() == "sonnet"


def test_default_is_resolved_from_the_live_constant_not_frozen(monkeypatch):
    """NIT/hygiene: `default` resolves the module constant at CALL time, so a
    runtime monkeypatch of DEFAULT_TIER is HONORED (not frozen at def time). The
    no-signal path now follows the patched default."""
    monkeypatch.setattr(model_tier, "DEFAULT_TIER", "haiku")
    assert recommend() == "haiku"
    assert recommend(phase="totally-unknown") == "haiku"
    # An EXPLICIT default= argument still wins over the (patched) constant.
    assert recommend(default="opus") == "opus"


# ── Difficulty overrides phase ──────────────────────────────────────────────


def test_difficulty_maps_to_expected_tier():
    assert recommend(difficulty="low") == "haiku"
    assert recommend(difficulty="medium") == "sonnet"
    assert recommend(difficulty="high") == "opus"


def test_difficulty_overrides_phase():
    # A `doc` phase defaults to haiku, but a high-difficulty estimate raises it.
    assert recommend(phase="doc", difficulty="high") == "opus"
    # A `design` phase defaults to opus, but a low-difficulty estimate lowers it
    # (difficulty is the stronger task-level signal than the phase default).
    assert recommend(phase="design", difficulty="low") == "haiku"


def test_difficulty_is_case_insensitive():
    assert recommend(difficulty="HIGH") == "opus"
    assert recommend(difficulty=" Low ") == "haiku"


def test_unknown_difficulty_is_ignored_falls_to_phase():
    # An unknown difficulty band is ignored — phase default applies.
    assert recommend(phase="design", difficulty="nonsense") == "opus"
    # ...and with no phase, it falls to the default (not a crash).
    assert recommend(difficulty="nonsense") == "sonnet"


def test_difficulty_tier_table_shape():
    assert DIFFICULTY_TIER == {"low": "haiku", "medium": "sonnet", "high": "opus"}


# ── ROLE_FLOOR: raises only, never lowers ───────────────────────────────────


def test_role_floor_raises_on_mechanical_phase():
    """A reviewer / security / architect / safety role on a HAIKU doc phase is
    raised to opus — the floor must not let a reviewer run shallow."""
    assert recommend(phase="doc", role_id="senior-reviewer-1") == "opus"
    assert recommend(phase="doc", role_id="security-engineer-1") == "opus"
    assert recommend(phase="doc", role_id="software-architect-1") == "opus"
    assert recommend(phase="doc", role_id="safety-officer-1") == "opus"


def test_role_floor_substring_is_case_insensitive():
    assert recommend(phase="doc", role_id="SECURITY-ENGINEER-1") == "opus"
    assert recommend(phase="status", role_id="Independent-Reviewer") == "opus"


def test_role_floor_never_lowers_a_high_base():
    """EXACT-MECHANISM: a HIGH base (opus) with a role whose floor is LOW must
    stay opus — the floor RAISES only, it can never downshift. (We have no
    sub-opus floors today; this asserts the mechanism via a high base + a role
    that does NOT match any opus floor, which must leave the high base intact.)"""
    # high difficulty → opus base; a non-floored role leaves it untouched.
    assert recommend(difficulty="high", role_id="backend-engineer-1") == "opus"
    # An opus phase + a non-floored implementer role stays opus.
    assert recommend(phase="design", role_id="backend-engineer-1") == "opus"


def test_role_floor_does_not_affect_unmatched_roles():
    # An implementer role on a haiku phase is NOT raised (no matching floor).
    assert recommend(phase="doc", role_id="backend-engineer-1") == "haiku"
    assert recommend(phase="doc", role_id="frontend-engineer-1") == "haiku"


# ── ROLE_FLOOR raise-only invariant made LOAD-BEARING (a) direct + (b) synthetic ─
#
# The raise-only guard is currently UNFALSIFIABLE through recommend() alone:
# every shipped ROLE_FLOOR entry is `opus`, so a floor can never be observed
# LOWERING a base (there is no base above opus). These two tests make the guard
# load-bearing: (a) hit `_more_capable` directly (the mechanism), and (b)
# monkeypatch a SUB-opus floor so a floor that COULD lower a high base is proven
# not to — and a floor that legitimately RAISES a low base is proven to.


def test_more_capable_keeps_the_higher_rank_either_argument_order():
    """DIRECT MECHANISM: `_more_capable` returns the more-capable tier regardless
    of argument order — this is the raise-only kernel. If it were mutated to
    return the LOWER rank (letting a floor downshift), these flip RED."""
    assert _more_capable("opus", "haiku") == "opus"  # base opus, low floor → opus
    assert _more_capable("haiku", "opus") == "opus"  # base haiku, high floor → opus
    # Ties keep the base; a None floor is a no-op (no constraint).
    assert _more_capable("sonnet", "sonnet") == "sonnet"
    assert _more_capable("opus", None) == "opus"
    assert _more_capable("haiku", None) == "haiku"


def test_synthetic_sub_opus_floor_raises_only_never_lowers(monkeypatch):
    """SYNTHETIC FLOOR: inject a SUB-opus floor `("worker","sonnet")` so the
    raise-only guard is observable end-to-end through recommend():

    * an OPUS phase base (`design`) must NOT be lowered to the sonnet floor —
      the floor RAISES only (this is the load-bearing anti-downshift assertion;
      it goes RED if `_more_capable` is mutated to let a floor lower a base);
    * a HAIKU phase base (`doc`) MUST be RAISED to the higher sonnet floor —
      proving the floor mechanism actually fires when the floor is higher.
    """
    monkeypatch.setattr(
        model_tier,
        "ROLE_FLOOR",
        [("worker", "sonnet"), *model_tier.ROLE_FLOOR],
    )
    # opus base, sonnet floor → stays opus (NOT lowered).
    assert recommend(phase="design", role_id="worker-1") == "opus"
    # haiku base, sonnet floor → RAISED to sonnet (the floor fires).
    assert recommend(phase="doc", role_id="worker-1") == "sonnet"
    # A NON-matching role on the haiku phase is untouched by the synthetic floor.
    assert recommend(phase="doc", role_id="backend-engineer-1") == "haiku"


def test_role_floor_table_shape():
    floors = dict(ROLE_FLOOR)
    assert floors["review"] == "opus"
    assert floors["security"] == "opus"
    assert floors["architect"] == "opus"
    assert floors["safety"] == "opus"


# ── override wins outright ──────────────────────────────────────────────────


def test_override_wins_outright_over_everything():
    # Override beats phase, difficulty, AND the role floor.
    assert recommend(phase="design", role_id="security-1", override="haiku") == "haiku"
    assert recommend(phase="doc", difficulty="low", override="opus") == "opus"


def test_invalid_override_is_ignored():
    # A garbage override is ignored; the phase default applies.
    assert recommend(phase="design", override="ultra") == "opus"
    assert recommend(phase="doc", override="") == "haiku"


# ── env ATELIER_MODEL_TIER pin ──────────────────────────────────────────────


def test_env_pin_wins_over_phase_and_floor():
    """The operator's global escape hatch: a valid env pin wins outright over
    phase/difficulty/floor (but NOT over an explicit per-call override)."""
    env = {ENV_TIER_VAR: "haiku"}
    assert recommend(phase="design", role_id="security-1", env=env) == "haiku"
    assert recommend(phase="review", env=env) == "haiku"


def test_explicit_override_beats_env_pin():
    env = {ENV_TIER_VAR: "haiku"}
    assert recommend(phase="doc", override="opus", env=env) == "opus"


def test_invalid_env_pin_is_ignored():
    """An invalid env value is ignored — resolution falls through to the phase
    default (a typo in the env var must NOT crash or wedge the run)."""
    env = {ENV_TIER_VAR: "turbo"}
    assert recommend(phase="design", env=env) == "opus"
    assert recommend(phase="doc", env=env) == "haiku"
    # blank env value is also ignored.
    assert recommend(phase="review", env={ENV_TIER_VAR: ""}) == "opus"


def test_env_none_means_no_pin():
    # Passing env=None (the default) means no pin is consulted at all.
    assert recommend(phase="design", env=None) == "opus"


# ── always returns a valid tier ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"phase": "garbage"},
        {"difficulty": "garbage"},
        {"override": "garbage"},
        {"env": {ENV_TIER_VAR: "garbage"}},
        {"phase": None, "role_id": None, "difficulty": None},
        {"role_id": "review", "phase": "garbage"},
    ],
)
def test_recommend_always_returns_a_valid_tier(kwargs):
    assert recommend(**kwargs) in TIERS
