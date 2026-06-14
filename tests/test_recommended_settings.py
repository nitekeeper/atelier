"""Tests for ``scripts.recommended_settings`` — the cost-posture settings +
version-upgrade eligibility module (the single source of truth for the
consent-gated "apply a recommended settings PROFILE on a version bump" feature).

The offer is now a NAMED-PROFILE CHOICE: three profiles (``cost-effective`` —
the default/recommended — ``balanced``, and ``code-quality``) plus skip. The
flow is profile-COUNT-agnostic (it iterates ``PROFILES`` and never assumes a
binary), so adding ``balanced`` (M6b-2) did not change its correctness. Each
profile manages
a fixed set of top-level keys + the ``CLAUDE_CODE_SUBAGENT_MODEL`` env key, and
applying a profile RECONCILES the managed keys (sets the profile's keys, removes
managed keys the profile does not specify, preserves all unmanaged keys + env
entries).

All tests are HERMETIC: ``CLAUDE_SETTINGS_PATH`` and
``ATELIER_SETTINGS_REC_STATE_PATH`` are monkeypatched into ``tmp_path`` so no
real ``~/.claude`` or ``~/.atelier`` file is read or written.

The assertions encode the load-bearing invariants and FAIL on a silent revert:
profile postures, managed-key reconciliation (both directions), env nested-merge
preserving unmanaged env keys, merge-safety for unmanaged top-level keys,
idempotency, version-gating, missing/malformed graceful read, read-only compute
paths, atomic write, old-state-file back-compat, and decision validation.
"""

from __future__ import annotations

import json

import pytest

from scripts import recommended_settings as rs


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point settings + state at tmp files; yield their paths."""
    settings = tmp_path / "claude" / "settings.json"
    state = tmp_path / "atelier" / "settings_rec_state.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("ATELIER_SETTINGS_REC_STATE_PATH", str(state))
    # Defensive: clear the dir-only override so it can't leak in.
    monkeypatch.delenv("ATELIER_STATE_DIR", raising=False)
    return settings, state


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── PROFILE postures (anti-revert pins) ────────────────────────────────────────


def test_profiles_registry_shape():
    """PROFILES holds the three named profiles; the default is cost-effective;
    the registry is ordered cheap → neutral → quality (default first)."""
    assert set(rs.PROFILES) == {"cost-effective", "balanced", "code-quality"}
    assert rs.DEFAULT_PROFILE == "cost-effective"
    # Ordered, default first, then balanced, then code-quality (the SKILL renders
    # the menu in this order).
    assert list(rs.PROFILES) == ["cost-effective", "balanced", "code-quality"]


def test_cost_effective_profile_exact():
    """cost-effective: sonnet orchestrator @ effortLevel high, haiku subagents,
    autoCompact on. It sets effortLevel and MUST NOT set ultracode.

    ANTI-REVERT: model is the family ALIAS (not a pinned claude-* id)."""
    assert rs.PROFILES["cost-effective"] == {
        "model": "sonnet",
        "effortLevel": "high",
        "autoCompactEnabled": True,
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
    }
    assert "ultracode" not in rs.PROFILES["cost-effective"]
    assert not rs.PROFILES["cost-effective"]["model"].startswith("claude-")


def test_balanced_profile_present_and_global_flow_three_profiles(hermetic, monkeypatch):
    """M6b-2 Iron-Law 3: PROFILES has all THREE (cost-effective / balanced /
    code-quality); the global settings-rec flow (maybe_offer / compute_changes /
    apply_profile) works with 3 and does NOT assume a binary; DEFAULT_PROFILE stays
    cost-effective.

    RED pre-fix: ``balanced`` is absent from PROFILES → the registry/membership
    assertions fail, so the test is RED until balanced is added."""
    settings, _ = hermetic
    # All three present; default unchanged.
    assert set(rs.PROFILES) == {"cost-effective", "balanced", "code-quality"}
    assert rs.DEFAULT_PROFILE == "cost-effective"

    # maybe_offer enumerates ALL THREE (not a binary) — ordered default-first.
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "2.0.0")
    offer = rs.maybe_offer()
    assert offer is not None
    assert list(offer["profiles"]) == ["cost-effective", "balanced", "code-quality"]
    # Each profile carries its posture + a computed diff (count-agnostic loop).
    for pid in ("cost-effective", "balanced", "code-quality"):
        assert offer["profiles"][pid]["posture"] == rs.PROFILES[pid]
        assert "set" in offer["profiles"][pid]["changes"]

    # compute_changes works for the NEW profile.
    bal_diff = rs.compute_changes({}, "balanced")
    assert bal_diff["set"]["model"] == "sonnet"
    assert bal_diff["set"]["effortLevel"] == "high"
    assert bal_diff["env_set"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}

    # apply_profile applies the balanced profile end to end (writes settings.json).
    applied = rs.apply_profile("balanced")
    assert applied["empty"] is False
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "sonnet"
    assert after["effortLevel"] == "high"
    assert "ultracode" not in after
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"

    # Switching balanced → code-quality clears the stale effortLevel (reconciler is
    # profile-count-agnostic — it removes managed keys the new profile omits).
    rs.apply_profile("code-quality")
    after2 = json.loads(settings.read_text(encoding="utf-8"))
    assert after2["model"] == "opus"
    assert after2["ultracode"] is True
    assert "effortLevel" not in after2

    # 'balanced' is a WRITE-valid decision token (the state validator accepts every
    # profile id — not just the original two).
    assert "balanced" in rs._valid_decisions()
    rs.write_state("2.0.0", "balanced")  # no raise


def test_balanced_profile_exact():
    """balanced (M6b-2): sonnet orchestrator @ effortLevel high (sets effortLevel,
    NOT ultracode — like cost-effective), sonnet subagents (no lean), autoCompact
    on. ANTI-REVERT: model is the family ALIAS (not a pinned claude-* id)."""
    assert rs.PROFILES["balanced"] == {
        "model": "sonnet",
        "effortLevel": "high",
        "autoCompactEnabled": True,
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    }
    assert "ultracode" not in rs.PROFILES["balanced"]
    assert not rs.PROFILES["balanced"]["model"].startswith("claude-")


def test_code_quality_profile_exact():
    """code-quality: opus orchestrator with ultracode (NOT effortLevel — the CLI
    resolver maps ultracode=True ⇒ xhigh effort), sonnet subagents, autoCompact
    on. It sets ultracode and MUST NOT set effortLevel."""
    assert rs.PROFILES["code-quality"] == {
        "model": "opus",
        "ultracode": True,
        "autoCompactEnabled": True,
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    }
    assert "effortLevel" not in rs.PROFILES["code-quality"]
    assert not rs.PROFILES["code-quality"]["model"].startswith("claude-")


def test_managed_key_sets_pinned():
    """The MANAGED top-level + env key sets are exactly these — the reconciler
    only ever touches keys in these sets."""
    assert {
        "model",
        "effortLevel",
        "ultracode",
        "autoCompactEnabled",
    } == rs.MANAGED_TOP_LEVEL_KEYS
    assert {"CLAUDE_CODE_SUBAGENT_MODEL"} == rs.MANAGED_ENV_KEYS


# ── compute_changes (read-only diff) ───────────────────────────────────────────


def test_compute_changes_fresh_cost_effective():
    """On empty settings, the cost-effective diff sets every managed key it
    specifies (including env.CLAUDE_CODE_SUBAGENT_MODEL) and removes nothing."""
    diff = rs.compute_changes({}, "cost-effective")
    assert diff["set"] == {
        "model": "sonnet",
        "effortLevel": "high",
        "autoCompactEnabled": True,
    }
    assert diff["env_set"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    assert diff["remove"] == []
    assert diff["empty"] is False


def test_compute_changes_reports_managed_key_removal():
    """Applying cost-effective over a code-quality posture REMOVES the stale
    mutually-exclusive ultracode key (and switches model/env), reported in the
    diff's ``remove`` list."""
    current = {
        "model": "opus",
        "ultracode": True,
        "autoCompactEnabled": True,
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    }
    diff = rs.compute_changes(current, "cost-effective")
    assert "ultracode" in diff["remove"]
    assert diff["set"]["model"] == "sonnet"
    assert diff["set"]["effortLevel"] == "high"
    assert diff["env_set"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    assert diff["empty"] is False


def test_compute_changes_empty_when_already_applied():
    """When the profile is already fully applied, the diff is empty (idempotent
    signal) — set/env_set/remove all empty and ``empty`` True."""
    current = dict(rs.PROFILES["cost-effective"])
    current["env"] = dict(rs.PROFILES["cost-effective"]["env"])
    diff = rs.compute_changes(current, "cost-effective")
    assert diff["set"] == {}
    assert diff["env_set"] == {}
    assert diff["remove"] == []
    assert diff["empty"] is True


def test_compute_changes_is_read_only():
    """compute_changes never mutates its argument."""
    current = {"model": "opus", "ultracode": True, "env": {"OTHER": "x"}}
    snapshot = json.loads(json.dumps(current))
    rs.compute_changes(current, "cost-effective")
    assert current == snapshot


# ── apply_profile — fresh apply + merge-safety ─────────────────────────────────


def test_apply_cost_effective_fresh(hermetic):
    """apply_profile('cost-effective') on empty settings writes exactly the
    profile's posture (managed keys + env)."""
    settings, _ = hermetic
    assert not settings.exists()

    applied = rs.apply_profile("cost-effective")
    assert applied["empty"] is False

    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after == {
        "model": "sonnet",
        "effortLevel": "high",
        "autoCompactEnabled": True,
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
    }


def test_apply_preserves_unmanaged_top_level_keys(hermetic):
    """apply_profile sets only the managed keys + the managed env key, leaving
    every unmanaged top-level key byte-identical.

    ANTI-REVERT: fails if a future change clobbers the whole file."""
    settings, _ = hermetic
    pre = {
        "enabledPlugins": ["atelier", "memex"],
        "permissions": {"allow": ["Bash(git status)"]},
        "statusLine": {"type": "command", "command": "echo hi"},
    }
    _write_json(settings, pre)

    rs.apply_profile("cost-effective")
    after = json.loads(settings.read_text(encoding="utf-8"))
    for k, v in pre.items():
        assert after[k] == v
    assert after["model"] == "sonnet"
    assert after["effortLevel"] == "high"
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


def test_apply_env_merge_preserves_unmanaged_env_key(hermetic):
    """The env block is nested-merged: a pre-existing unmanaged env key
    (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS) survives while
    CLAUDE_CODE_SUBAGENT_MODEL is set.

    ANTI-REVERT: fails if apply replaces the whole env block instead of merging."""
    settings, _ = hermetic
    _write_json(
        settings,
        {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "FOO": "bar"}},
    )

    rs.apply_profile("cost-effective")
    after = json.loads(settings.read_text(encoding="utf-8"))
    # Unmanaged env keys preserved.
    assert after["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    assert after["env"]["FOO"] == "bar"
    # Managed env key set.
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


def test_apply_does_not_introduce_empty_env_when_none_before():
    """If there was no env block and (hypothetically) the profile set no env key,
    no empty env key is introduced. Both real profiles set an env key, so this
    guards the helper's contract directly."""
    merged = rs._reconcile({}, {"model": "sonnet"}, {})
    assert "env" not in merged


def test_apply_non_dict_env_is_treated_as_absent_no_crash(hermetic):
    """PINNED behavior: a NON-dict ``env`` (already invalid for the harness) is
    treated as ABSENT and replaced with a fresh well-formed managed env. The
    apply does NOT crash and the malformed value is dropped (there is no
    well-formed prior value to merge)."""
    settings, _ = hermetic
    _write_json(settings, {"env": "not-a-dict", "enabledPlugins": ["atelier"]})

    rs.apply_profile("cost-effective")  # no raise
    after = json.loads(settings.read_text(encoding="utf-8"))
    # The malformed env was replaced with the managed env object.
    assert after["env"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    # Unmanaged top-level keys still preserved.
    assert after["enabledPlugins"] == ["atelier"]


def test_reconcile_non_dict_env_replaced_directly():
    """_reconcile pins the same non-dict-env contract at the unit level: a
    string env is treated as absent and replaced with the managed env."""
    merged = rs._reconcile(
        {"env": 123, "model": "opus"},
        {"model": "sonnet"},
        {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
    )
    assert merged["env"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    assert merged["model"] == "sonnet"


# ── MANAGED-KEY reconciliation — both directions ───────────────────────────────


def test_reconcile_cost_to_quality_clears_effortLevel_adds_ultracode(hermetic):
    """Switching cost-effective → code-quality clears the stale effortLevel key
    and adds ultracode (the mutually-exclusive managed pair is reconciled)."""
    settings, _ = hermetic
    rs.apply_profile("cost-effective")
    pre = json.loads(settings.read_text(encoding="utf-8"))
    assert pre["effortLevel"] == "high"
    assert "ultracode" not in pre

    rs.apply_profile("code-quality")
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "opus"
    assert after["ultracode"] is True
    assert "effortLevel" not in after  # stale managed key cleared
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"


def test_reconcile_quality_to_cost_clears_ultracode_adds_effortLevel(hermetic):
    """Switching code-quality → cost-effective clears ultracode and adds
    effortLevel (reconciliation the other direction)."""
    settings, _ = hermetic
    rs.apply_profile("code-quality")
    pre = json.loads(settings.read_text(encoding="utf-8"))
    assert pre["ultracode"] is True
    assert "effortLevel" not in pre

    rs.apply_profile("cost-effective")
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "sonnet"
    assert after["effortLevel"] == "high"
    assert "ultracode" not in after  # stale managed key cleared
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


def test_reconcile_preserves_unmanaged_env_across_switch(hermetic):
    """A pre-existing unmanaged env key survives a profile SWITCH (cost→quality),
    not just a fresh apply."""
    settings, _ = hermetic
    _write_json(settings, {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}})
    rs.apply_profile("cost-effective")
    rs.apply_profile("code-quality")
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"


# ── IDEMPOTENCY ────────────────────────────────────────────────────────────────


def test_apply_is_idempotent(hermetic):
    """Re-applying the SAME profile is a no-op (empty diff) and leaves the file
    byte-identical."""
    settings, _ = hermetic
    rs.apply_profile("cost-effective")
    first_bytes = settings.read_bytes()

    again = rs.apply_profile("cost-effective")
    assert again["empty"] is True
    assert settings.read_bytes() == first_bytes


def test_apply_recommended_back_compat_applies_default(hermetic):
    """The thin apply_recommended back-compat wrapper applies the DEFAULT
    profile (cost-effective)."""
    settings, _ = hermetic
    rs.apply_recommended()
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "sonnet"
    assert after["effortLevel"] == "high"
    assert after["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


# ── VERSION-GATING + eligibility payload schema ────────────────────────────────


def test_eligibility_payload_schema(hermetic, monkeypatch):
    """maybe_offer() carries eligible, current_version, default_profile, and a
    per-profile ``profiles`` map (each with the profile posture + its diff). It
    also carries a top-level ``changes`` convenience diff for the default
    profile (the entry-skill 'non-empty changes' gate)."""
    # Empty settings ⇒ every profile would change something.
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.9.0")

    offer = rs.maybe_offer()
    assert offer is not None
    assert offer["eligible"] is True
    assert offer["current_version"] == "1.9.0"
    assert offer["default_profile"] == "cost-effective"
    # Per-profile info, ordered default-first — ALL of PROFILES (now three).
    assert list(offer["profiles"]) == ["cost-effective", "balanced", "code-quality"]
    ce = offer["profiles"]["cost-effective"]
    assert ce["posture"] == rs.PROFILES["cost-effective"]
    assert ce["changes"]["set"]["model"] == "sonnet"
    assert ce["changes"]["env_set"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    cq = offer["profiles"]["code-quality"]
    assert cq["posture"] == rs.PROFILES["code-quality"]
    assert cq["changes"]["set"]["ultracode"] is True
    # Top-level convenience diff = the default profile's diff.
    assert offer["changes"] == ce["changes"]


def test_eligibility_payload_posture_is_a_copy_not_the_constant(hermetic, monkeypatch):
    """DEFENSE-IN-DEPTH: the offer's per-profile ``posture`` is a DEEP COPY, not
    the live PROFILES constant. A consumer mutating the returned payload must
    NOT corrupt the module constant.

    ANTI-REVERT: fails if the payload ever exposes PROFILES by reference."""
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.9.0")
    offer = rs.maybe_offer()
    assert offer is not None

    posture = offer["profiles"]["cost-effective"]["posture"]
    # Equal in value but NOT the same object (nor the same nested env object).
    assert posture == rs.PROFILES["cost-effective"]
    assert posture is not rs.PROFILES["cost-effective"]
    assert posture["env"] is not rs.PROFILES["cost-effective"]["env"]

    # Mutating the returned payload leaves the module constant intact.
    posture["model"] = "MUTATED"
    posture["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = "MUTATED"
    assert rs.PROFILES["cost-effective"]["model"] == "sonnet"
    assert rs.PROFILES["cost-effective"]["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


def test_version_gating_once_per_version(hermetic, monkeypatch):
    """After write_state(v, decision), maybe_offer() is None for v; bumping the
    plugin version makes it eligible again."""
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")

    assert rs.maybe_offer() is not None

    rs.write_state("1.5.0", "declined")
    assert rs.maybe_offer() is None

    # A NEW version re-offers.
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.6.0")
    again = rs.maybe_offer()
    assert again is not None
    assert again["current_version"] == "1.6.0"


def test_version_gating_profile_decision_records(hermetic, monkeypatch):
    """write_state(v, 'cost-effective') records the version so it does not
    re-prompt."""
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")
    assert rs.maybe_offer() is not None
    rs.write_state("1.5.0", "cost-effective")
    assert rs.maybe_offer() is None


def test_eligibility_none_when_no_profile_would_change(hermetic, monkeypatch):
    """When NO profile would change anything (cost-effective already fully
    applied AND code-quality keys are a subset — impossible together, so we use
    a settings file where the default profile is applied: code-quality still
    differs, so it IS eligible). To get a true None, both profiles must be
    no-ops, which can only happen if neither set/env/remove differs. We force
    that by stubbing compute_changes to always-empty."""
    settings, _ = hermetic
    _write_json(settings, dict(rs.PROFILES["cost-effective"]))
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "9.9.9")
    # cost-effective is applied but code-quality still differs ⇒ eligible.
    assert rs.maybe_offer() is not None

    # Now force every profile diff to empty ⇒ None.
    monkeypatch.setattr(
        rs,
        "compute_changes",
        lambda current, profile: {"set": {}, "env_set": {}, "remove": [], "empty": True},
    )
    assert rs.maybe_offer() is None


def test_eligibility_none_when_version_unreadable(hermetic, monkeypatch):
    """A None plugin version (missing/malformed manifest) yields no offer."""
    monkeypatch.setattr(rs, "current_plugin_version", lambda: None)
    assert rs.maybe_offer() is None


# ── MISSING / MALFORMED settings.json ──────────────────────────────────────────


def test_missing_settings_file(hermetic):
    """Missing settings.json → load_settings == {}; apply creates it."""
    settings, _ = hermetic
    assert rs.load_settings(settings) == {}
    rs.apply_profile("cost-effective")
    assert settings.exists()


def test_malformed_settings_file_graceful(hermetic):
    """Invalid JSON → load_settings == {} and apply does not raise."""
    settings, _ = hermetic
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not valid json,,,", encoding="utf-8")
    assert rs.load_settings(settings) == {}
    rs.apply_profile("cost-effective")  # no raise
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "sonnet"


# ── READ-ONLY compute paths (byte-invariant) ───────────────────────────────────


def test_compute_paths_never_write(hermetic, monkeypatch):
    """eligibility() / maybe_offer() / compute_changes() leave settings.json and
    the state file BYTE-UNCHANGED. Only apply_profile / write_state mutate.

    ANTI-REVERT: fails if a write ever leaks into a compute/eligibility path."""
    settings, state = hermetic
    _write_json(settings, {"env": {"X": "1"}})
    _write_json(state, {"last_handled_version": "0.0.1", "decision": "declined"})

    settings_bytes = settings.read_bytes()
    state_bytes = state.read_bytes()

    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")
    rs.eligibility()
    rs.maybe_offer()
    rs.compute_changes(rs.load_settings(settings), "cost-effective")
    rs.compute_changes(rs.load_settings(settings), "code-quality")
    rs.read_state()

    assert settings.read_bytes() == settings_bytes
    assert state.read_bytes() == state_bytes


# ── ATOMIC write ───────────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_partial_file(hermetic):
    """After apply, the target dir contains exactly settings.json — no .tmp /
    partial file lingers."""
    settings, _ = hermetic
    rs.apply_profile("cost-effective")
    leftovers = sorted(p.name for p in settings.parent.iterdir())
    assert leftovers == ["settings.json"]
    assert not any(p.name.endswith(".tmp") for p in settings.parent.iterdir())


def test_write_state_atomic_no_partial(hermetic):
    """write_state likewise leaves no .tmp debris."""
    _, state = hermetic
    rs.write_state("1.5.0", "cost-effective")
    leftovers = sorted(p.name for p in state.parent.iterdir())
    assert leftovers == ["settings_rec_state.json"]


# ── state read/write round-trip + validation ───────────────────────────────────


def test_state_roundtrip(hermetic):
    assert rs.read_state() == {}  # missing → {}
    rs.write_state("2.0.0", "code-quality")
    assert rs.read_state() == {"last_handled_version": "2.0.0", "decision": "code-quality"}


def test_write_state_accepts_profile_ids_and_declined(hermetic):
    """write_state accepts each profile id and 'declined'."""
    for decision in ("cost-effective", "code-quality", "declined"):
        payload = rs.write_state("1.5.0", decision)
        assert payload["decision"] == decision


def test_write_state_rejects_garbage(hermetic):
    """A decision that is neither a known profile id nor 'declined' is rejected."""
    with pytest.raises(ValueError):
        rs.write_state("1.5.0", "maybe")
    with pytest.raises(ValueError):
        rs.write_state("1.5.0", "applied")  # the OLD token is no longer a valid WRITE


def test_read_state_tolerates_old_format(hermetic, monkeypatch):
    """An OLD-format state file (decision 'applied' / 'declined') must not break
    read_state or eligibility — only last_handled_version is read for gating."""
    _, state = hermetic
    _write_json(state, {"last_handled_version": "1.5.0", "decision": "applied"})
    # read_state returns it verbatim without KeyError.
    assert rs.read_state() == {"last_handled_version": "1.5.0", "decision": "applied"}
    # eligibility gates on last_handled_version regardless of the legacy token.
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")
    assert rs.maybe_offer() is None  # same version ⇒ gated, no crash


def test_read_state_malformed_graceful(hermetic):
    _, state = hermetic
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("garbage{{", encoding="utf-8")
    assert rs.read_state() == {}


# ── state_path env-override precedence (ATELIER_STATE_DIR) ──────────────────────


def test_state_path_dir_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_SETTINGS_REC_STATE_PATH", raising=False)
    monkeypatch.setenv("ATELIER_STATE_DIR", str(tmp_path / "custom"))
    assert rs.state_path() == tmp_path / "custom" / "settings_rec_state.json"


# ── current_plugin_version reads the real manifest ─────────────────────────────


def test_current_plugin_version_reads_manifest():
    """The live manifest version is a parseable X.Y.Z string (smoke that the
    resolver anchors on the plugin root, not CWD)."""
    ver = rs.current_plugin_version()
    assert ver is not None
    parts = ver.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
