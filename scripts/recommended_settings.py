"""Recommended settings PROFILES + version-upgrade eligibility.

This module is the SINGLE SOURCE OF TRUTH for the opt-in, consent-gated
"apply a recommended settings PROFILE on a version upgrade" feature.

When atelier's plugin version is bumped, the FIRST session on the new
version OFFERS (consent-gated) a choice among the named profiles for the
user's global ``~/.claude/settings.json``. The presented default is the
recommended ``cost-effective`` profile: pressing Enter / an empty answer
APPLIES it (informed consent — the menu states this explicitly), while an
explicit skip writes nothing. (This keystroke→action mapping lives wholly in
``internal/settings-recommendation/SKILL.md``; this module is consent-agnostic
— it never sees the user's input and only acts on the explicit
``apply_profile`` / ``write_state`` calls the SKILL makes.)

The profiles (ordered cheap → neutral → quality; every consent/reconcile/
eligibility path iterates :data:`PROFILES` and is profile-COUNT-agnostic — it
does NOT assume a binary, so the offer may present TWO or THREE):

  * ``cost-effective`` (the DEFAULT / recommended profile) —
    orchestrator ``model: "sonnet"`` at ``effortLevel: "high"``; subagents
    via ``env.CLAUDE_CODE_SUBAGENT_MODEL: "haiku"``; ``autoCompactEnabled``.
  * ``balanced`` (the neutral MIDDLE, M6b-2) — orchestrator ``model: "sonnet"``
    at ``effortLevel: "high"`` (sets ``effortLevel``, NOT ``ultracode`` — like
    cost-effective); subagents via ``env.CLAUDE_CODE_SUBAGENT_MODEL: "sonnet"``
    (no lean either way); ``autoCompactEnabled``. The R-MODE ``balanced`` run
    mode maps onto this profile. Values are a sane neutral middle and TUNABLE.
  * ``code-quality`` (optional) — orchestrator ``model: "opus"`` with
    ``ultracode: true`` (NOT ``effortLevel`` — see the harness facts below);
    subagents via ``env.CLAUDE_CODE_SUBAGENT_MODEL: "sonnet"``;
    ``autoCompactEnabled``.

All ``model`` / subagent-model values use the version-resilient family
ALIASES (``"sonnet"`` / ``"opus"`` / ``"haiku"``), NOT pinned
``claude-sonnet-4-6`` ids — so the recommendation survives model
re-versioning without a code change.

HARNESS GROUND TRUTH (verified against the live CLI — do not deviate):

  * settings.json ``model`` is a free-form string; the opus/sonnet/haiku
    aliases are accepted and version-resilient. We emit aliases.
  * settings.json ``effortLevel`` is a strict enum {low, medium, high,
    xhigh}. ``"ultracode"`` is NOT a valid effortLevel — it is a SEPARATE
    top-level boolean key. The CLI resolver is ``if ultracode === true:
    effort = "xhigh" else effortLevel``. So ``code-quality`` sets
    ``"ultracode": true`` and MUST NOT set effortLevel; ``cost-effective``
    sets ``"effortLevel": "high"`` and MUST NOT set ultracode. The two keys
    are mutually exclusive, which is why the writer RECONCILES them.
  * The subagent MODEL is controlled by the env var
    ``CLAUDE_CODE_SUBAGENT_MODEL`` in settings.json's top-level ``"env"``
    object (which may already hold unmanaged keys such as
    ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` — those MUST be preserved).
  * There is NO per-subagent EFFORT knob anywhere in the harness. Subagent
    control is MODEL-ONLY; we never attempt to set subagent effort.

Design constraints (load-bearing — this writes to a user's GLOBAL settings):

  * **Pure + testable.** No interactive I/O lives here; the consent menu
    belongs to the orchestrating skill
    (``internal/settings-recommendation/SKILL.md``). Python computes and
    (only on the explicit ``apply_profile`` call) writes.
  * **Read-only compute paths.** ``eligibility`` / ``maybe_offer`` /
    ``compute_changes`` / ``load_settings`` / ``read_state`` NEVER write.
    Only ``apply_profile`` (+ its thin ``apply_recommended`` wrapper) and
    ``write_state`` mutate disk.
  * **Managed-key reconciliation.** ``apply_profile`` sets every key the
    chosen profile specifies, REMOVES managed top-level keys the profile
    does NOT specify (so switching cost↔quality clears the stale
    mutually-exclusive key), nested-merges the ``env`` block (preserving
    every unmanaged env key while setting CLAUDE_CODE_SUBAGENT_MODEL), and
    leaves every UNMANAGED top-level key untouched.
  * **Atomic.** Writes go through a temp file in the same dir + ``os.replace``
    so a reader never sees a partial file and no ``.tmp`` debris remains.
  * **Idempotent.** A re-apply of the same already-applied profile is a no-op.
  * **Never crashes a session.** Missing / malformed files degrade to ``{}``
    on read; ``apply_profile`` catches file errors and returns an empty diff
    rather than raising.
  * **Never writes managed-settings.json.** Only the user
    ``~/.claude/settings.json`` is ever touched; enterprise-managed policy is
    out of scope (and may override these — the offer text says so).

Env overrides (for hermetic tests + non-default installs):

  * ``CLAUDE_SETTINGS_PATH`` — overrides the settings.json target path.
  * ``ATELIER_SETTINGS_REC_STATE_PATH`` — overrides the state file path.
  * ``ATELIER_STATE_DIR`` — overrides only the state DIRECTORY (state file is
    ``<dir>/settings_rec_state.json``); mirrors ``scripts/scope.py``'s
    ``.atelier`` state-dir convention.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# ── The single canonical source of truth: named profiles ───────────────────────
#
# Each profile is the FULL managed posture it expresses. All model/subagent
# values are version-resilient family ALIASES (NOT pinned claude-* ids —
# repinning to a versioned id is a regression the tests catch). cost-effective
# is the DEFAULT/recommended profile.
#
# NOTE the mutually-exclusive managed pair: cost-effective sets `effortLevel`
# and NOT `ultracode`; code-quality sets `ultracode` (⇒ xhigh effort via the CLI
# resolver) and NOT `effortLevel`. The writer reconciles this — applying one
# clears the other's stale key.
COST_EFFECTIVE: dict[str, Any] = {
    "model": "sonnet",
    "effortLevel": "high",
    "autoCompactEnabled": True,
    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
}

# BALANCED — the neutral MIDDLE profile (M6b-2). Sonnet orchestrator at
# effortLevel high (NO ultracode — so it sets `effortLevel`, NOT the
# mutually-exclusive `ultracode` key, exactly like cost-effective), sonnet
# subagents (no lean either way). It is the orchestrator-model family + subagent
# posture for the R-MODE `balanced` run mode (scripts/run_mode.py). The values are
# a sane neutral middle and are TUNABLE — a maintainer re-tunes the spread here
# without touching the reconciler/eligibility logic (which is profile-COUNT-agnostic).
BALANCED: dict[str, Any] = {
    "model": "sonnet",  # TUNABLE: neutral orchestrator family (advisory via R-MODE)
    "effortLevel": "high",  # TUNABLE: sets effortLevel (NOT ultracode) — like cost-effective
    "autoCompactEnabled": True,
    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},  # TUNABLE: neutral subagent (no lean)
}

CODE_QUALITY: dict[str, Any] = {
    "model": "opus",
    "ultracode": True,
    "autoCompactEnabled": True,
    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
}

# Ordered, DEFAULT FIRST — the SKILL renders the menu in this order and marks
# the default as recommended. `balanced` sits BETWEEN cost-effective and
# code-quality (cheap → neutral → quality). All consent/reconcile/eligibility
# paths iterate PROFILES and are profile-COUNT-agnostic (they do NOT assume a
# binary), so adding `balanced` does not change the global settings-rec flow's
# correctness — it may now offer THREE profiles, which is acceptable.
PROFILES: dict[str, dict[str, Any]] = {
    "cost-effective": COST_EFFECTIVE,
    "balanced": BALANCED,
    "code-quality": CODE_QUALITY,
}
DEFAULT_PROFILE = "cost-effective"

# The top-level keys these profiles MANAGE. The reconciler sets the profile's
# managed keys and REMOVES managed keys the profile does not specify; it NEVER
# touches a key outside this set. `ultracode` and `effortLevel` are the
# mutually-exclusive pair.
MANAGED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"model", "effortLevel", "ultracode", "autoCompactEnabled"}
)
# The env keys these profiles manage. Every OTHER env key (e.g.
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS) is preserved untouched.
MANAGED_ENV_KEYS: frozenset[str] = frozenset({"CLAUDE_CODE_SUBAGENT_MODEL"})

_STATE_DIR_NAME = ".atelier"  # mirrors scripts/scope.py._STATE_DIR_NAME
_STATE_FILE_NAME = "settings_rec_state.json"


# ── Path resolution (env-overridable; no hardcoded /home/<user>) ───────────────


def settings_path() -> Path:
    """Resolve the user's global Claude settings file.

    ``CLAUDE_SETTINGS_PATH`` env override (hermetic tests + non-default
    installs) wins; otherwise ``~/.claude/settings.json`` via ``Path.home()``.
    """
    override = os.environ.get("CLAUDE_SETTINGS_PATH")
    if override and override.strip():
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


def state_path() -> Path:
    """Resolve the per-version state file ``~/.atelier/settings_rec_state.json``.

    Resolution order: ``ATELIER_SETTINGS_REC_STATE_PATH`` (full path) →
    ``ATELIER_STATE_DIR`` (directory; file appended) → ``~/.atelier/…``.
    Mirrors ``scripts/scope.py``'s ``.atelier`` state-dir convention.
    """
    override = os.environ.get("ATELIER_SETTINGS_REC_STATE_PATH")
    if override and override.strip():
        return Path(override)
    state_dir = os.environ.get("ATELIER_STATE_DIR")
    if state_dir and state_dir.strip():
        return Path(state_dir) / _STATE_FILE_NAME
    return Path.home() / _STATE_DIR_NAME / _STATE_FILE_NAME


def current_plugin_version() -> str | None:
    """Read the INSTALLED atelier plugin version from its manifest.

    The manifest lives at ``.claude-plugin/plugin.json`` relative to the
    plugin ROOT — resolved as ``Path(__file__).resolve().parents[1]`` (the
    same anchor ``scripts/bootstrap.py`` uses for the atelier plugin root),
    NEVER a ``Path.cwd()`` walk (CWD is the user's target project, not the
    plugin). Returns ``None`` on missing / malformed manifest so a fresh or
    broken install never crashes the pre-flight.
    """
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, str) and version.strip():
        return version
    return None


# ── Settings read + change computation (read-only) ─────────────────────────────


def load_settings(path: Path) -> dict:
    """Parse the settings JSON at ``path``; return ``{}`` on a missing file OR
    malformed JSON. NEVER raises — a broken settings.json must not crash a
    session's pre-flight.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def compute_changes(current: dict, profile: str = DEFAULT_PROFILE) -> dict:
    """Return the READ-ONLY diff a profile would make to ``current``.

    The diff schema (consumed by the SKILL for rendering and by
    ``eligibility`` for the already-applied check)::

        {
          "set":     {<managed top-level key>: <new value>, ...},  # absent/differs
          "env_set": {"CLAUDE_CODE_SUBAGENT_MODEL": <value>}|{}     # absent/differs
          "remove":  [<managed top-level key>, ...],               # stale, to clear
          "empty":   <bool>,   # True ⇒ profile already fully applied (no-op)
        }

    * ``set`` — managed top-level keys the profile specifies whose value is
      ABSENT or DIFFERS from ``current``.
    * ``env_set`` — the managed env key (CLAUDE_CODE_SUBAGENT_MODEL) when it is
      absent or differs in ``current['env']``.
    * ``remove`` — managed top-level keys PRESENT in ``current`` that the chosen
      profile does NOT specify (the stale mutually-exclusive key, e.g.
      ``ultracode`` when applying cost-effective). These are cleared on apply.
    * ``empty`` — True iff nothing would change (idempotent signal).

    Read-only — never mutates either argument.
    """
    posture = PROFILES[profile]
    wanted_top = {k: v for k, v in posture.items() if k != "env"}
    wanted_env = posture.get("env", {})

    set_diff = {k: v for k, v in wanted_top.items() if current.get(k) != v}

    current_env = current.get("env") if isinstance(current.get("env"), dict) else {}
    env_set = {
        k: v for k, v in wanted_env.items() if k in MANAGED_ENV_KEYS and current_env.get(k) != v
    }

    # Managed top-level keys present today but NOT specified by this profile.
    remove = sorted(k for k in MANAGED_TOP_LEVEL_KEYS if k in current and k not in wanted_top)

    empty = not set_diff and not env_set and not remove
    return {"set": set_diff, "env_set": env_set, "remove": remove, "empty": empty}


# ── Per-version state (a mutator besides apply_profile) ─────────────────────────


def read_state() -> dict:
    """Read the per-version state file; ``{}`` on missing / malformed. Read-only.

    Tolerates OLD-format files (``decision`` of ``"applied"``/``"declined"``) —
    it is returned verbatim. ``eligibility`` only consults
    ``last_handled_version``, so a legacy ``decision`` token never breaks gating.
    """
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _valid_decisions() -> set[str]:
    """The WRITE-valid decision tokens: the profile ids plus ``"declined"``.

    NOTE the OLD ``"applied"`` token is no longer WRITE-valid (a profile id is
    written instead); ``read_state`` still TOLERATES an old file containing it.
    """
    return set(PROFILES) | {"declined"}


def write_state(version: str, decision: str) -> dict:
    """Record that ``version`` was handled with ``decision`` so the SAME version
    never re-prompts. Creates the state dir if missing, then writes ATOMICALLY
    (temp file + ``os.replace``). Returns the persisted payload.

    ``decision`` is validated against the PROFILE IDS plus ``'declined'`` to keep
    the state file well-formed (a typo would silently break the
    never-re-prompt semantics). State stays ``{last_handled_version, decision}``.
    """
    valid = _valid_decisions()
    if decision not in valid:
        raise ValueError(f"decision must be one of {sorted(valid)} (got {decision!r})")
    payload = {"last_handled_version": version, "decision": decision}
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, payload)
    return payload


# ── Eligibility / offer (read-only) ────────────────────────────────────────────


def eligibility() -> dict | None:
    """Return the offer payload iff a version upgrade leaves SOME profile with a
    change to make, else ``None``. READ-ONLY — consults the manifest, settings,
    and state file without writing any of them.

    Eligible iff ALL hold:
      1. ``current_plugin_version()`` is not None (manifest readable), AND
      2. it differs from ``read_state()['last_handled_version']`` (a bump or a
         first-ever run), AND
      3. AT LEAST ONE profile's ``compute_changes`` diff is non-empty (there is
         actually something to apply for some profile — if EVERY profile is
         already fully applied, the offer is silent).

    Payload schema::

        {
          "eligible":        True,
          "current_version": <ver>,
          "default_profile": "cost-effective",
          "profiles": {                # ordered default-first; ALL of PROFILES
            "cost-effective": {"posture": <profile dict>, "changes": <diff>},
            "balanced":       {"posture": <profile dict>, "changes": <diff>},
            "code-quality":   {"posture": <profile dict>, "changes": <diff>},
          },
          "changes": <diff>,           # convenience: the DEFAULT profile's diff
        }

    The per-profile ``changes`` diff has the schema documented on
    ``compute_changes``; the SKILL renders a readable menu from it.
    """
    version = current_plugin_version()
    if version is None:
        return None
    if read_state().get("last_handled_version") == version:
        return None

    current = load_settings(settings_path())
    profiles: dict[str, dict[str, Any]] = {}
    any_change = False
    for pid, posture in PROFILES.items():
        diff = compute_changes(current, pid)
        if not diff["empty"]:
            any_change = True
        # DEEP-COPY the posture into the payload (defense-in-depth): the offer is
        # READ-ONLY data, so a consumer must never be able to mutate the live
        # PROFILES module constant through `offer[...]["posture"]`.
        profiles[pid] = {"posture": copy.deepcopy(posture), "changes": diff}

    if not any_change:
        return None

    return {
        "eligible": True,
        "current_version": version,
        "default_profile": DEFAULT_PROFILE,
        "profiles": profiles,
        "changes": profiles[DEFAULT_PROFILE]["changes"],
    }


def maybe_offer() -> dict | None:
    """Alias for :func:`eligibility` — the name the startup pre-flight calls.
    READ-ONLY: returns the offer payload or ``None``; never mutates disk.
    """
    return eligibility()


# ── The explicit write entry points ─────────────────────────────────────────


def _reconcile(current: dict, wanted_top: dict, wanted_env: dict) -> dict:
    """Build the merged settings dict for a profile, RECONCILING managed keys.

    * Carry through EVERY top-level key, then for the managed set: set the
      profile's keys and DROP managed keys the profile does not specify (clears
      the stale mutually-exclusive key).
    * Nested-merge ``env``: start from a copy of the current env (preserving all
      unmanaged keys such as CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS), then set the
      profile's managed env keys. If the resulting env is empty AND there was no
      env before, do NOT introduce an empty ``env`` key. A NON-dict ``env`` (e.g.
      a string) is treated as ABSENT and replaced with a fresh managed env — it
      is already invalid for the harness (settings.json ``env`` must be an
      object), so there is nothing well-formed to preserve. This never raises.
    * UNMANAGED top-level keys are untouched.

    Pure — does not mutate ``current``.
    """
    merged = dict(current)
    # Reconcile managed top-level keys.
    for k in MANAGED_TOP_LEVEL_KEYS:
        if k in wanted_top:
            merged[k] = wanted_top[k]
        else:
            merged.pop(k, None)  # clear a stale managed key the profile omits

    # Nested-merge env: preserve unmanaged env keys, set the managed one(s).
    # An `env` that is not a dict is INTENTIONALLY treated as absent (had_env
    # False): a non-dict env is invalid for the harness, so the reconciler
    # replaces it with a well-formed managed env rather than crashing — there is
    # no well-formed prior value to merge. (Covered by test_apply_non_dict_env_*.)
    current_env = current.get("env")
    had_env = isinstance(current_env, dict)
    new_env = dict(current_env) if had_env else {}
    for k, v in wanted_env.items():
        if k in MANAGED_ENV_KEYS:
            new_env[k] = v
    if new_env:
        merged["env"] = new_env
    elif not had_env:
        # Nothing to set and no env before ⇒ do not introduce an empty env key.
        merged.pop("env", None)
    else:
        merged["env"] = new_env
    return merged


def apply_profile(profile_id: str = DEFAULT_PROFILE, path: Path | None = None) -> dict:
    """RECONCILE the chosen profile into the user's settings.json, written
    ATOMICALLY. Returns the diff that was applied (``compute_changes`` shape;
    ``empty`` True ⇒ no-op).

    Sets every key the profile specifies, REMOVES managed top-level keys the
    profile omits (clearing the stale mutually-exclusive key on a cost↔quality
    switch), nested-merges ``env`` (preserving unmanaged env keys), and leaves
    every unmanaged top-level key untouched.

    Robustness: a fresh ``~/.claude`` is created (mkdir parents). Any file error
    is caught and surfaced as an empty diff rather than crashing the session —
    this runs inside a startup pre-flight where a write failure must degrade,
    not abort.
    """
    if profile_id not in PROFILES:
        raise ValueError(f"unknown profile {profile_id!r}; expected one of {sorted(PROFILES)}")
    target = path if path is not None else settings_path()
    current = load_settings(target)
    diff = compute_changes(current, profile_id)
    if diff["empty"]:
        return diff

    posture = PROFILES[profile_id]
    wanted_top = {k: v for k, v in posture.items() if k != "env"}
    wanted_env = posture.get("env", {})
    merged = _reconcile(current, wanted_top, wanted_env)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, merged)
    except OSError:
        # Never crash a session on a settings write failure — degrade to no-op.
        return {"set": {}, "env_set": {}, "remove": [], "empty": True}
    return diff


def apply_recommended(path: Path | None = None) -> dict:
    """Thin back-compat wrapper: apply the DEFAULT profile (``cost-effective``).

    Retained so any older caller (and the back-compat test) keeps working; new
    callers — including the SKILL — call :func:`apply_profile` with the chosen
    profile id.
    """
    return apply_profile(DEFAULT_PROFILE, path)


# ── Atomic JSON write helper ───────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Serialize ``payload`` to ``path`` atomically: temp file in the SAME dir
    → ``os.replace``. A reader never sees a partial file and no ``.tmp`` debris
    remains on success. On any failure the orphan temp file is best-effort
    removed and the original exception re-raised.
    """
    fd, tmp = tempfile.mkstemp(prefix=".settings-rec-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
