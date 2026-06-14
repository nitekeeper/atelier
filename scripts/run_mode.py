"""RunMode — the per-run cost/quality posture for the deterministic-host engine.

M6b-2 (R-MODE — the config/UX half of M6b). At ``atelier:run`` START the operator
selects ONE of THREE run modes and the choice is honored for the WHOLE run, fanning
out to four levers:

  (a) the per-task ``model_tier`` POSTURE (cost-lean / neutral / opus-lean — a
      post-base transform in :func:`scripts.model_tier.recommend`);
  (b) the :class:`~scripts.budget_pool.BudgetPool` ceiling / headroom;
  (c) ``static_fleet_width`` / ``max_workers`` (the fan-out cap);
  (d) an ADVISORY orchestrator-model recommendation (NEVER written to settings.json
      — the running session can't change its own model mid-run; R-MODE surfaces a
      recommendation only).

The three modes and their mapping onto the SINGLE source of truth
:data:`scripts.recommended_settings.PROFILES` (we do NOT fork the model lists):

    cost-lean    → ``cost-effective`` profile  + posture ``cost-lean``
    balanced     → ``balanced`` profile        + posture ``neutral``
    quality-lean → ``code-quality`` profile    + posture ``opus-lean``

**The default tracks the SAVED profile, NOT balanced.** The no-higher-rung default
is the mode mapped to :data:`scripts.recommended_settings.DEFAULT_PROFILE` —
currently ``cost-effective`` → **cost-lean** (see :func:`default_mode_id`). So an
unanswered prompt / non-interactive run resolves to ``cost-lean`` (a NON-neutral
mode that biases tiers down + narrows the budget/fleet), NOT to ``balanced``.
``balanced`` is ONLY the explicitly-neutral no-op mode (the one that leaves every
lever byte-identical to the pre-R-MODE host wiring).

The orchestrator model family per mode is read from ``PROFILES[<profile>]['model']``
— so a re-tune of a profile's orchestrator model automatically re-tunes R-MODE,
and there is exactly one place model families live.

R-MODE is **PER-RUN / transient**. It is ORTHOGONAL to the once-per-version global
settings-recommendation flow (``recommended_settings.maybe_offer`` / ``apply_profile``,
which is the SOLE writer of ``~/.claude/settings.json``). Resolving / applying a
RunMode NEVER writes settings.json — :func:`resolve_run_mode` is pure (it reads
``PROFILES`` and an injectable ``env`` only) and returns a value object the run
threads through ``run_host_pipeline_for_project``.

Resolution precedence (highest first), see :func:`resolve_run_mode`:

    1. explicit ``explicit=`` arg              — a programmatic override (wins outright)
    2. ``interactive_choice=`` arg             — the operator's START-prompt answer
    3. env ``ATELIER_RUN_MODE``                — the operator's global escape hatch
    4. saved-profile default                   — the mode mapped to the SAVED profile
       (:data:`scripts.recommended_settings.DEFAULT_PROFILE` → :func:`default_mode_id`),
       resolved WITHOUT prompting. NEVER blocks a non-interactive / CI run.

:func:`resolve_run_mode` does NO I/O — it cannot block, so it cannot prompt. The
always-prompt-vs-silent decision (TTY / CI-marker detection) lives in the SKILL
prose (``skills/run/SKILL.md`` → "Run mode selection — R-MODE"), which detects the
interactive context and passes ``interactive_choice`` (or nothing, for a silent
non-interactive resolve). This module just maps a (possibly absent) choice through
the precedence above.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from scripts import recommended_settings

# ── Vocabulary ──────────────────────────────────────────────────────────────

#: The three run-mode ids the operator chooses among (ordered cheapest → priciest).
COST_LEAN = "cost-lean"
BALANCED = "balanced"
QUALITY_LEAN = "quality-lean"
MODE_IDS: tuple[str, ...] = (COST_LEAN, BALANCED, QUALITY_LEAN)

#: The env var an operator sets to pin ONE run mode (precedence rung 3 — the
#: global escape hatch, mirroring ``ATELIER_MODEL_TIER`` / ``ATELIER_TRANSPORT``).
ENV_RUN_MODE_VAR = "ATELIER_RUN_MODE"

#: mode_id → (recommended_settings PROFILE id, model_tier POSTURE). The profile id
#: is the SINGLE source of the orchestrator model family (read from
#: ``PROFILES[<profile>]['model']`` — never re-listed here); the posture is the
#: per-task model_tier transform applied in ``model_tier.recommend``.
_MODE_TO_PROFILE_AND_POSTURE: dict[str, tuple[str, str]] = {
    COST_LEAN: ("cost-effective", "cost-lean"),
    BALANCED: ("balanced", "neutral"),
    QUALITY_LEAN: ("code-quality", "opus-lean"),
}

# ── Budget / fleet levers per mode (TUNABLE) ────────────────────────────────
#
# These are the (b) BudgetPool and (c) fleet-width levers. They are the SECOND
# tunable surface of R-MODE (the FIRST being the posture transform in model_tier).
# FLAGGED TUNABLE: a maintainer re-shapes the cost/quality spread by editing these
# three rows without touching the resolution logic.
#
#   * budget_headroom — the headroom fraction handed to BudgetPool (default 0.70).
#     A tighter headroom (cost-lean) reserves a bigger drift buffer → a SMALLER
#     effective ceiling for the same total; a looser headroom (quality-lean) spends
#     more of the total. balanced == the BudgetPool default 0.70 (so balanced is the
#     no-op identity vs today's host path).
#   * budget_ceiling_factor — multiplies the caller-supplied ``total_tokens`` to form
#     the run's effective total. cost-lean < 1.0 (a smaller pool), quality-lean > 1.0
#     (a bigger pool), balanced == 1.0 (identity — no rescale).
#   * max_workers — the fan-out CAP fed to ``static_fleet_width`` (it only ever
#     NARROWS the engine MAX_PARALLEL_WORKERS=5; None == "use the caller's
#     max_workers", the identity for balanced).
_MODE_BUDGET_HEADROOM: dict[str, float] = {
    COST_LEAN: 0.55,  # TUNABLE: tighter buffer ⇒ smaller effective ceiling
    BALANCED: 0.70,  # == BudgetPool default ⇒ no-op identity vs today
    QUALITY_LEAN: 0.85,  # TUNABLE: looser buffer ⇒ spends more of the total
}
_MODE_BUDGET_CEILING_FACTOR: dict[str, float] = {
    COST_LEAN: 0.60,  # TUNABLE: a smaller token pool overall
    BALANCED: 1.0,  # identity — no rescale of the caller's total
    QUALITY_LEAN: 1.50,  # TUNABLE: a larger token pool overall
}
_MODE_MAX_WORKERS: dict[str, int | None] = {
    COST_LEAN: 2,  # TUNABLE: narrow fan-out (fewer concurrent agents)
    BALANCED: None,  # use the caller's max_workers (identity)
    QUALITY_LEAN: 5,  # TUNABLE: full fan-out up to the engine cap
}


@dataclass(frozen=True)
class RunMode:
    """The resolved per-run cost/quality posture (immutable value object).

    Fields
    ------
    mode_id:
        One of :data:`MODE_IDS` (``cost-lean`` / ``balanced`` / ``quality-lean``).
    posture:
        The per-task model_tier posture (``cost-lean`` / ``neutral`` / ``opus-lean``)
        threaded into :func:`scripts.model_tier.recommend`. ``neutral`` is a no-op
        (byte-identical to today's tier output).
    profile_id:
        The :data:`scripts.recommended_settings.PROFILES` id this mode maps onto —
        the SINGLE source of the orchestrator model family (we never re-list models).
    orchestrator_model:
        ADVISORY-ONLY orchestrator model family alias (read from
        ``PROFILES[profile_id]['model']``). Surfaced as a recommendation; R-MODE
        NEVER writes it to settings.json.
    budget_headroom:
        Headroom fraction for the run's :class:`~scripts.budget_pool.BudgetPool`.
    budget_ceiling_factor:
        Multiplier applied to the caller-supplied ``total_tokens`` to size the run's
        pool. ``balanced`` == 1.0 (identity).
    max_workers:
        Fan-out cap for ``static_fleet_width`` (``None`` == use the caller's cap —
        the ``balanced`` identity). It only ever NARROWS the engine ceiling.
    """

    mode_id: str
    posture: str
    profile_id: str
    orchestrator_model: str
    budget_headroom: float
    budget_ceiling_factor: float
    max_workers: int | None

    @property
    def is_neutral(self) -> bool:
        """True iff this mode applies NO transform to any lever (the ``balanced``
        identity): neutral posture, default headroom 0.70, ceiling factor 1.0, and
        no max_workers narrowing. The host entrypoint uses this to keep an
        EXPLICITLY-NEUTRAL run mode (``balanced``) a byte-for-byte no-op vs the
        pre-M6b-2 wiring. NOTE: ``run_mode=None`` is NOT neutral by default — it
        auto-resolves to the saved-profile default (cost-effective → cost-lean),
        which IS non-neutral; only ``balanced`` (or another lever-neutral mode)
        satisfies this.
        """
        return (
            self.posture == "neutral"
            and self.budget_ceiling_factor == 1.0
            and self.budget_headroom == 0.70
            and self.max_workers is None
        )

    def budget_total_for(self, base_total_tokens: int) -> int:
        """Scale a caller-supplied ``total_tokens`` by this mode's ceiling factor.

        ``balanced`` (factor 1.0) returns ``base_total_tokens`` unchanged. Floors to
        an int and clamps to >= 1 so the resulting :class:`BudgetPool` constructor
        (which requires a positive total) never raises on a tiny base.
        """
        return max(1, int(base_total_tokens * self.budget_ceiling_factor))


def _build_run_mode(mode_id: str) -> RunMode:
    """Construct the :class:`RunMode` for a validated *mode_id*.

    Reads the orchestrator model family from
    ``recommended_settings.PROFILES[<profile>]['model']`` — the SINGLE source — so a
    profile re-tune flows here automatically and there is no forked model list.
    """
    profile_id, posture = _MODE_TO_PROFILE_AND_POSTURE[mode_id]
    profile = recommended_settings.PROFILES[profile_id]
    orchestrator_model = str(profile.get("model", ""))
    return RunMode(
        mode_id=mode_id,
        posture=posture,
        profile_id=profile_id,
        orchestrator_model=orchestrator_model,
        budget_headroom=_MODE_BUDGET_HEADROOM[mode_id],
        budget_ceiling_factor=_MODE_BUDGET_CEILING_FACTOR[mode_id],
        max_workers=_MODE_MAX_WORKERS[mode_id],
    )


def default_mode_id() -> str:
    """The mode_id the SAVED profile (``recommended_settings.DEFAULT_PROFILE``) maps
    onto — the non-interactive / CI default.

    Inverts :data:`_MODE_TO_PROFILE_AND_POSTURE` so the default tracks
    ``DEFAULT_PROFILE`` without a second hardcoded constant. ``DEFAULT_PROFILE`` is
    ``cost-effective`` today → ``cost-lean``. If a future ``DEFAULT_PROFILE`` has no
    mapped mode, falls back to :data:`BALANCED` (defensive — never raises).
    """
    default_profile = recommended_settings.DEFAULT_PROFILE
    for mode_id, (profile_id, _posture) in _MODE_TO_PROFILE_AND_POSTURE.items():
        if profile_id == default_profile:
            return mode_id
    return BALANCED


def _valid_mode(value: str | None) -> str | None:
    """Return *value* iff it is a known mode_id (case/space-normalized), else None.

    Defensive: an invalid explicit/interactive/env value is IGNORED (falls through
    to the next precedence rung), never raised — a typo in ``ATELIER_RUN_MODE`` must
    not crash a run.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    return token if token in _MODE_TO_PROFILE_AND_POSTURE else None


def resolve_run_mode(
    *,
    explicit: str | None = None,
    interactive_choice: str | None = None,
    env: Mapping[str, str] | None = None,
) -> RunMode:
    """Resolve the run's :class:`RunMode` by the documented precedence.

    PURE: reads only ``recommended_settings.PROFILES`` + the injectable ``env`` (no
    settings.json write, no other disk I/O). Always returns a valid :class:`RunMode`.

    Precedence (highest first):

    1. ``explicit`` — a programmatic override (if a valid mode id) wins outright.
    2. ``interactive_choice`` — the operator's START-prompt answer (if valid).
    3. env ``ATELIER_RUN_MODE`` — the operator's global escape hatch (if valid).
    4. **saved-profile default** — the mode mapped to the SAVED profile
       (:data:`recommended_settings.DEFAULT_PROFILE` → :func:`default_mode_id`),
       resolved WITHOUT prompting. This is the final fallback when no higher rung
       supplied a valid choice — whether that is an interactive operator who pressed
       Enter for the default, OR a non-interactive / CI run that threaded nothing.
       Currently ``cost-effective`` → ``cost-lean`` (a NON-neutral mode), so an
       unanswered / non-threaded resolve does NOT yield a neutral no-op — it yields
       the saved cost-lean posture.

    This function does NO I/O — it cannot block, so it cannot prompt. The
    always-prompt-vs-silent decision (TTY / CI-marker detection) lives in the SKILL
    prose, which detects the interactive context and passes ``interactive_choice``
    (or nothing). A non-interactive / CI context simply means no higher rung is
    supplied, so it falls through to the saved-profile default.
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env

    # 1. explicit programmatic override.
    chosen = _valid_mode(explicit)
    # 2. the operator's interactive START-prompt answer.
    if chosen is None:
        chosen = _valid_mode(interactive_choice)
    # 3. env ATELIER_RUN_MODE — global escape hatch.
    if chosen is None:
        chosen = _valid_mode(resolved_env.get(ENV_RUN_MODE_VAR))
    # 4. saved-profile default (any unfilled rung) — the mode mapped to
    #    DEFAULT_PROFILE (currently cost-effective → cost-lean, NON-neutral),
    #    resolved silently (no prompt — the always-prompt is the SKILL's job).
    if chosen is None:
        chosen = default_mode_id()

    return _build_run_mode(chosen)
