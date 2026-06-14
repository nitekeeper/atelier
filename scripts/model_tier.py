# scripts/model_tier.py
"""Atelier per-task model-tier selection — the policy brain.

Atelier spawns many teammates/subagents per cycle. Defaulting EVERY spawn to
the most-capable (and most-expensive) model is wasteful: a mechanical
doc/agenda/status task does not need Opus. This module is the pure, fully
unit-tested policy that picks a model TIER per task by DIFFICULTY, reserving the
top tier for reasoning/review-heavy work and routing mechanical work to cheaper
tiers. "Future atelier manages this automatically."

The Agent tool's ``model`` param accepts the TIER ALIASES directly
(version-agnostic), so this module emits the alias — never a pinned version id.

Tier → model-id mapping (for operators; the Agent tool ALSO accepts the bare
aliases, which is exactly what :func:`recommend` returns):

    haiku   = claude-haiku-4-5    (cheapest; mechanical phases)
    sonnet  = claude-sonnet-4-6   (the safe middle DEFAULT)
    opus    = claude-opus-4-8     (reasoning / review / security / architect)

Everything below is DATA-DRIVEN and intended to be TUNABLE: the maintainer edits
:data:`PHASE_TIER`, :data:`ROLE_FLOOR`, and :data:`DIFFICULTY_TIER` to re-shape
the cost/quality posture without touching the resolution logic in
:func:`recommend`. The operator's global escape hatch is the
``ATELIER_MODEL_TIER`` env var (pin one tier for the whole run).

Resolution precedence (see :func:`recommend`):

    1. explicit ``override`` (if a valid tier)            — wins outright
    2. env ``ATELIER_MODEL_TIER`` (if a valid tier)       — operator escape hatch
    3. base = DIFFICULTY_TIER[difficulty] if given,
              else PHASE_TIER[normalize(phase)] if known,
              else ``default`` (sonnet)
    4. ROLE_FLOOR: raise base to the floor of any matching role (RAISES only)

The floor only ever RAISES the tier — an independent reviewer / security /
architect / safety role must never be downshifted below Opus (it must catch what
the implementer missed). The default is **sonnet, NOT opus** — the whole point is
to stop defaulting to the expensive tier when no signal exists.

.. note::
   ``difficulty`` is **RESERVED — not yet emitted by the planner**. There is no
   ``difficulty`` DB column and no planner code sets it, so ``task.get("difficulty")``
   is always ``None`` on a real run and rung 3's difficulty branch is currently
   DEAD in production. It is kept in the precedence (forward-compatible) so a
   future planner can light it up without a code change. The ACTIVE signals today
   are **phase + role-floor**.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The model-tier aliases, ordered cheapest → most-capable. These are exactly the
#: strings the Agent tool's ``model`` param accepts (version-agnostic), so this
#: tuple is both the validation set AND the emitted vocabulary.
TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")

#: Rank map for "more-capable-of" comparisons. Higher rank == more capable. The
#: floor logic (step 4) keeps the MAX-rank of (base, role-floor); ties keep base.
_RANK: dict[str, int] = {tier: i for i, tier in enumerate(TIERS)}

#: The env var an operator sets to pin ONE tier for the entire run — the global
#: escape hatch (precedence 2, below an explicit per-call override).
ENV_TIER_VAR: str = "ATELIER_MODEL_TIER"

#: The safe middle default when NO signal (phase/difficulty/override/env) exists.
#: Deliberately SONNET, not opus: the load-bearing cost guarantee is that a plain
#: signal-free task does NOT spawn the expensive tier.
DEFAULT_TIER: str = "sonnet"

#: Difficulty band → tier. RESERVED — the planner does NOT yet emit a
#: ``difficulty`` (there is no DB column and no planner code sets one), so this
#: rung is presently DEAD in production; ``task.get("difficulty")`` is always
#: ``None`` on a real run. Kept forward-compatible: when a known difficulty IS
#: supplied it takes precedence over the phase default (step 3). The ACTIVE base
#: signal today is the phase.
DIFFICULTY_TIER: dict[str, str] = {
    "low": "haiku",
    "medium": "sonnet",
    "high": "opus",
}

#: Dev-arc phase → default tier (TUNABLE). Keyed by a NORMALIZED phase token
#: (see :func:`normalize_phase`): lowercased, ``:state``/``-state`` suffix
#: stripped, separators unified. Reasoning/judgement/high-stakes phases map to
#: ``opus``; medium implementation/verification phases to ``sonnet``; purely
#: mechanical phases to ``haiku``.
PHASE_TIER: dict[str, str] = {
    # ── opus: reasoning / judgement / high-stakes ──────────────────────────
    "design": "opus",
    "plan": "opus",
    "security": "opus",
    "review": "opus",
    "handoff": "opus",
    "diagnose": "opus",
    "tdd:red": "opus",  # test DESIGN — choosing the right failing test is judgement
    "abandonment": "opus",  # abandon / no-consensus decision
    "no-consensus": "opus",
    # ── sonnet: medium implementation / verification ───────────────────────
    "tdd": "sonnet",
    "tdd:green": "sonnet",
    "tdd:clean": "sonnet",  # post-green cleanup/refactor — medium verification
    "qa": "sonnet",
    "verify": "sonnet",
    "receive-review": "sonnet",
    # ── haiku: mechanical ──────────────────────────────────────────────────
    "doc": "haiku",
    "agenda": "haiku",
    "status": "haiku",
    "format": "haiku",
}

#: The R-MODE posture vocabulary (M6b-2). A posture is a POST-BASE transform
#: applied to the resolved base tier (difficulty/phase/default) BEFORE the
#: ROLE_FLOOR — it biases the base cost↔quality WITHOUT forking the tier policy:
#:
#:   * ``cost-lean``  — cap the base DOWN one rung (toward haiku/sonnet);
#:   * ``neutral``    — NO transform (byte-identical to the pre-posture behavior —
#:                      so the R-MODE ``balanced`` mode / ``posture=None`` is a no-op);
#:   * ``opus-lean``  — raise the base UP one rung (toward opus).
#:
#: The transform is a ONE-RUNG shift (clamped to the TIERS range), deliberately
#: gentle so a posture nudges the spread rather than collapsing every task to one
#: tier (the operator's ``ATELIER_MODEL_TIER`` env pin remains the hard one-tier
#: lever, and it sits ABOVE the posture in precedence). CRITICAL: the posture is
#: applied to the BASE only; the ROLE_FLOOR (review/security/architect/safety →
#: opus) is applied AFTER and stays a HARD floor in ALL THREE postures — a
#: cost-lean run NEVER downshifts a floored role below opus.
POSTURE_COST_LEAN = "cost-lean"
POSTURE_NEUTRAL = "neutral"
POSTURE_OPUS_LEAN = "opus-lean"
VALID_POSTURES: frozenset[str] = frozenset({POSTURE_COST_LEAN, POSTURE_NEUTRAL, POSTURE_OPUS_LEAN})

#: Role-id substring → MINIMUM tier (a FLOOR that only RAISES, never lowers).
#: An independent reviewer / security / architect / safety role must never run
#: below Opus — it has to catch what the implementer missed. Matching is by
#: case-insensitive SUBSTRING on the role-id (e.g. ``security-engineer-1`` matches
#: the ``security`` floor). Order is irrelevant (the floor is the max over all
#: matches), but listed most-specific-intent-first for readability. TUNABLE.
ROLE_FLOOR: list[tuple[str, str]] = [
    ("review", "opus"),
    ("security", "opus"),
    ("architect", "opus"),
    ("safety", "opus"),
]


def _valid_tier(value: str | None) -> str | None:
    """Return ``value`` iff it is a known tier alias, else ``None`` (defensive).

    Used to validate the explicit override and the env pin: an invalid/garbage
    value is IGNORED (falls through to the next precedence rung), never raised —
    a typo in the env var must not crash a dispatch.
    """
    if isinstance(value, str) and value in _RANK:
        return value
    return None


def normalize_phase(phase: str | None) -> str | None:
    """Normalize a dev-arc phase token to the key form used by :data:`PHASE_TIER`.

    The phase a caller hands us is shaped many ways. The PRODUCTION form is the
    ``phases`` table id ``<base>:<state>`` returned by ``get_phase`` — e.g.
    ``"design:open"``, ``"plan:approved"``, ``"review:approved"``,
    ``"review:changes-requested"``, ``"tdd:red"``, ``"tdd:green"``,
    ``"tdd:clean"``, ``"handoff:complete"``. A caller may ALSO pass the
    ``dev:<base>`` phase-GROUP form (``"dev:review"``, ``"dev:tdd"``), a
    bare key (``"tdd"``), or a hyphenated variant (``"tdd-green"``). We resolve
    in this order — and CRUCIALLY do NOT unconditionally rewrite ``-`` → ``:``,
    which would corrupt the multi-word keys ``no-consensus`` / ``receive-review``
    (``"no-consensus:reached"`` must resolve to ``no-consensus``, NOT ``no``):

    1. lowercase + strip surrounding whitespace; empty → ``None``;
    2. strip a leading ``dev:`` / ``dev-`` namespace prefix if present, so the
       phase-group form maps to its base (``"dev:review"`` → ``review``,
       ``"dev:tdd"`` → ``tdd``);
    3. if the token is itself an exact :data:`PHASE_TIER` key, return it (covers
       compound keys stored verbatim — ``tdd:green``, ``tdd:red``,
       ``no-consensus``, ``receive-review``);
    4. try the hyphen→colon UNIFIED form (``"tdd-green"`` → ``tdd:green``) and
       return it if THAT is a key — applied as a whole-token attempt, never an
       unconditional rewrite, so it cannot split a hyphenated key;
    5. split on ``:`` and try progressively SHORTER colon-prefixes,
       longest-first, so the most-specific known key wins
       (``"no-consensus:reached"`` → ``no-consensus``; ``"review:approved"`` →
       ``review``; ``"tdd:in-progress"`` → ``tdd``);
    6. else return the bare base (first ``:``-component) so :func:`recommend`
       falls through to the default — never raise.

    Returns the resolved key string, or ``None`` for an empty/None input (so the
    caller falls through to the default — never a crash).
    """
    if not isinstance(phase, str):
        return None
    token = phase.strip().lower()
    if not token:
        return None

    # (2) Strip a leading `dev:` / `dev-` namespace prefix (the phase-GROUP form)
    # so `dev:review` → `review`, `dev:tdd` → `tdd`. Only the prefix is stripped;
    # the remainder is then resolved exactly like a bare phase id.
    for prefix in ("dev:", "dev-"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
            break

    # (3) Exact key wins outright — this covers compound keys stored verbatim
    # (`tdd:green`, `tdd:red`) AND the multi-word hyphen keys (`no-consensus`,
    # `receive-review`) that a blanket `-`→`:` rewrite would have corrupted.
    if token in PHASE_TIER:
        return token

    # (4) Whole-token hyphen→colon unification (`tdd-green` → `tdd:green`). Done
    # as a single key lookup, NOT an in-place rewrite, so it can only MATCH a
    # colon-key — it can never split `no-consensus` into `no:consensus`.
    unified = token.replace("-", ":")
    if unified in PHASE_TIER:
        return unified

    # (5) Longest-first colon-prefix walk: try `a:b:c`, then `a:b`, then `a`, so
    # the most-specific known key wins (`no-consensus:reached` → `no-consensus`).
    parts = token.split(":")
    for i in range(len(parts), 0, -1):
        candidate = ":".join(parts[:i])
        if candidate in PHASE_TIER:
            return candidate

    # (6) Unknown — return the bare base so recommend() falls to the default
    # (defensive: no crash).
    return parts[0] or None


def _phase_tier(phase: str | None) -> str | None:
    """Return the :data:`PHASE_TIER` entry for ``phase`` (normalized), else None."""
    key = normalize_phase(phase)
    if key is None:
        return None
    return PHASE_TIER.get(key)


def _role_floor(role_id: str | None) -> str | None:
    """Return the MOST-CAPABLE matching :data:`ROLE_FLOOR` tier for ``role_id``.

    A role-id may match more than one floor substring; we keep the max-rank floor
    so the strictest constraint wins. Matching is case-insensitive substring.
    Returns ``None`` when no floor matches (no constraint to apply).
    """
    if not isinstance(role_id, str) or not role_id:
        return None
    needle = role_id.lower()
    best: str | None = None
    for sub, floor in ROLE_FLOOR:
        if sub in needle and (best is None or _RANK[floor] > _RANK[best]):
            best = floor
    return best


def _more_capable(a: str, b: str | None) -> str:
    """Return whichever of ``a`` / ``b`` has the higher rank (``b`` may be None).

    Ties (or ``b is None``) keep ``a`` — the floor RAISES only, never lowers, so
    a base that already meets/exceeds the floor is unchanged.
    """
    if b is None:
        return a
    return b if _RANK[b] > _RANK[a] else a


def _apply_posture(base: str, posture: str | None) -> str:
    """Bias *base* by the R-MODE *posture* — a ONE-RUNG shift clamped to TIERS.

    Applied to the BASE tier (difficulty/phase/default) AFTER the base is resolved
    and BEFORE the ROLE_FLOOR (so the floor can still raise a posture-capped role
    back to its hard minimum). Pure — reads only :data:`TIERS` / :data:`_RANK`.

    * ``cost-lean`` → shift DOWN one rung (``max(0, rank-1)``) — toward haiku;
    * ``opus-lean`` → shift UP one rung (``min(last, rank+1)``) — toward opus;
    * ``neutral`` / ``None`` / any unknown value → NO shift (returns *base*
      unchanged — so a neutral/absent posture is byte-identical to today).

    The clamp means a base already at the cheapest tier under cost-lean (or the
    priciest under opus-lean) is returned unchanged — the shift never wraps.
    """
    if posture == POSTURE_COST_LEAN:
        return TIERS[max(0, _RANK[base] - 1)]
    if posture == POSTURE_OPUS_LEAN:
        return TIERS[min(len(TIERS) - 1, _RANK[base] + 1)]
    # neutral / None / unknown — no transform (forward-defensive: an unrecognized
    # posture is IGNORED rather than raising, mirroring the env/override tolerance).
    return base


def recommend(
    *,
    phase: str | None = None,
    role_id: str | None = None,
    difficulty: str | None = None,
    override: str | None = None,
    default: str | None = None,
    env: Mapping[str, str] | None = None,
    posture: str | None = None,
) -> str:
    """Recommend a model TIER alias (``"haiku"`` | ``"sonnet"`` | ``"opus"``).

    ALWAYS returns a valid tier string — the function is defensive end to end and
    never raises on bad input.

    Precedence (highest first):

    1. **explicit ``override``** — if it is a valid tier, it wins outright (the
       per-call escape hatch). An invalid override is IGNORED.
    2. **env ``ATELIER_MODEL_TIER``** — if set to a valid tier, it wins outright
       (the operator's global escape hatch). An invalid/blank value is IGNORED.
       ``env`` defaults to ``None`` (no pin); pass a ``Mapping`` (e.g.
       ``os.environ``) to honor it.
    3. **base tier** — ``DIFFICULTY_TIER[difficulty]`` if a known difficulty is
       given (difficulty is the strongest task-level signal); else the phase
       default ``PHASE_TIER[normalize(phase)]`` if the phase is known; else
       ``default`` (the live :data:`DEFAULT_TIER`, sonnet). An UNKNOWN difficulty
       is ignored (falls to phase); an UNKNOWN phase falls to ``default`` (neither
       crashes). NOTE: ``difficulty`` is **reserved — not yet emitted by the
       planner** (no DB column, no planner code sets it), so on a real run this
       rung is always reached via the PHASE branch; the difficulty branch is kept
       forward-compatible but is presently DEAD in production. The ACTIVE base
       signal today is phase.
    3b. **R-MODE posture** (M6b-2) — a one-rung cost↔quality bias applied to the
       BASE from rung 3, AFTER the base and BEFORE the ROLE_FLOOR: ``cost-lean``
       caps DOWN, ``opus-lean`` raises UP, ``neutral`` / ``None`` is a NO-OP
       (byte-identical to the pre-posture behavior). It sits BELOW the env pin
       (rung 2): ``ATELIER_MODEL_TIER`` returns outright and is NEVER posture-
       adjusted (the env pin stays the operator's hard one-tier escape hatch above
       the posture). It sits ABOVE the floor so a floored role is ALWAYS raised
       back to opus even under ``cost-lean``.
    4. **ROLE_FLOOR** — raise the POSTURE-ADJUSTED base to the floor of any matching
       role (``_more_capable``). The floor only RAISES: a base that already
       meets/exceeds the floor is unchanged, and a high base is NEVER lowered by a
       low floor. CRITICAL ORDERING: the floor is applied AFTER the posture, so
       ``cost-lean`` can never downshift a review/security/architect/safety role
       below opus — the floor wins (proven by
       ``test_rmode_posture_caps_tier_but_role_floor_stays_hard``).

    ``default`` defaults to ``None``, meaning "use the live module-level
    :data:`DEFAULT_TIER`"; it is resolved INSIDE the body so a runtime
    monkeypatch of the constant is honored (not frozen at def time). An explicit
    ``default=`` argument still wins.
    """
    # Resolve `default` from the module constant at CALL time (not at def time):
    # binding it as the parameter default would freeze the value, so a runtime
    # monkeypatch of DEFAULT_TIER would be silently ignored. `None` means "use
    # the live module constant"; an explicit `default=` still wins.
    default = DEFAULT_TIER if default is None else default

    # (1) explicit override wins outright.
    ov = _valid_tier(override)
    if ov is not None:
        return ov

    # (2) env pin wins outright (operator global escape hatch).
    if env is not None:
        pin = _valid_tier(env.get(ENV_TIER_VAR))
        if pin is not None:
            return pin

    # (3) base from difficulty (strongest task signal), else phase, else default.
    base: str | None = None
    if difficulty is not None:
        base = (
            DIFFICULTY_TIER.get(difficulty.strip().lower()) if isinstance(difficulty, str) else None
        )
    if base is None:
        base = _phase_tier(phase)
    if base is None:
        base = default if _valid_tier(default) is not None else DEFAULT_TIER

    # (3b) R-MODE posture — bias the BASE cost↔quality (one-rung shift), applied
    # AFTER the base and BEFORE the floor. neutral/None is a no-op. The env pin
    # (rung 2) already returned outright above, so the posture never overrides it.
    base = _apply_posture(base, posture)

    # (4) apply the role floor — RAISES only. Applied AFTER the posture, so a
    # floored role (review/security/architect/safety → opus) is ALWAYS raised back
    # to opus even when cost-lean capped its base down — the floor is HARD in all
    # three postures.
    return _more_capable(base, _role_floor(role_id))
