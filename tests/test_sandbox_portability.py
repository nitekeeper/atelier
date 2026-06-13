"""Cross-platform native-sandbox detection + messaging tests.

Pins the portability fix that makes the deterministic-host engine's mandatory
SANDBOX gating correct on BOTH Linux/WSL2 (bubblewrap + socat) and macOS (built-in
Seatbelt, zero installs), instead of the old Linux-only ``shutil.which("bwrap")``
heuristic that wrongly reported "unavailable" on a Mac.

These tests drive the DETECTION + MESSAGING only — the fail-closed
mandatory-sandbox gate (``UnsandboxedRealRunError``) is unchanged and tested in
``tests/test_cli_security.py``. Platform is simulated by monkeypatching
``sys.platform`` + ``shutil.which`` (the helpers read both at call time).
"""

from __future__ import annotations

import json

import scripts.cli_dispatch as cd
from scripts.cli_dispatch import (
    bwrap_sandbox_wrap,
    native_sandbox_wrap,
    sandbox_prereq_status,
    sandbox_runtime_available,
)


def _patch_platform(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(cd.sys, "platform", platform)


def _patch_which(monkeypatch, *, present: set[str]) -> None:
    """Make ``shutil.which`` (as the module sees it) report only ``present`` tools."""

    def fake_which(name, *a, **k):
        return f"/usr/bin/{name}" if name in present else None

    monkeypatch.setattr(cd.shutil, "which", fake_which)


# ── darwin: always available (Seatbelt built-in, zero installs) ─────────────


def test_darwin_available_even_with_no_bwrap_or_socat(monkeypatch):
    _patch_platform(monkeypatch, "darwin")
    # Nothing on PATH — macOS must STILL report available (Seatbelt is built-in).
    _patch_which(monkeypatch, present=set())
    available, reason = sandbox_prereq_status()
    assert available is True
    assert sandbox_runtime_available() is True
    assert "Seatbelt" in reason
    assert "built-in" in reason.lower()


# ── linux: available iff BOTH bwrap AND socat present ───────────────────────


def test_linux_available_with_both_bwrap_and_socat(monkeypatch):
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present={"bwrap", "socat"})
    available, reason = sandbox_prereq_status()
    assert available is True
    assert sandbox_runtime_available() is True
    assert "bubblewrap" in reason or "bwrap" in reason
    assert "socat" in reason


def test_linux_unavailable_when_bwrap_missing(monkeypatch):
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present={"socat"})  # bwrap absent
    available, reason = sandbox_prereq_status()
    assert available is False
    assert sandbox_runtime_available() is False
    assert "bubblewrap" in reason or "bwrap" in reason
    # the install hint names both packages
    assert "socat" in reason
    assert "apt install" in reason


def test_linux_unavailable_when_socat_missing(monkeypatch):
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present={"bwrap"})  # socat absent
    available, reason = sandbox_prereq_status()
    assert available is False
    assert sandbox_runtime_available() is False
    assert "socat" in reason
    assert "apt install" in reason


def test_linux_unavailable_when_both_missing(monkeypatch):
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present=set())
    available, reason = sandbox_prereq_status()
    assert available is False
    assert sandbox_runtime_available() is False
    # both are named as missing
    assert "socat" in reason
    assert "bubblewrap" in reason or "bwrap" in reason


def test_wsl2_reports_as_linux_path(monkeypatch):
    """WSL2 reports ``sys.platform == 'linux'`` and therefore takes the
    bwrap+socat path (NOT the win32 path)."""
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present={"bwrap", "socat"})
    assert sandbox_runtime_available() is True


# ── win32: native Windows is unsupported → unavailable + WSL2 pointer ────────


def test_win32_unavailable_with_wsl2_recommendation(monkeypatch):
    _patch_platform(monkeypatch, "win32")
    # Even if bwrap/socat somehow resolved, native Windows is unsupported.
    _patch_which(monkeypatch, present={"bwrap", "socat"})
    available, reason = sandbox_prereq_status()
    assert available is False
    assert sandbox_runtime_available() is False
    assert "Windows" in reason
    assert "WSL2" in reason


# ── unknown platform: conservative → unavailable ────────────────────────────


def test_unknown_platform_is_unavailable(monkeypatch):
    _patch_platform(monkeypatch, "sunos5")
    _patch_which(monkeypatch, present={"bwrap", "socat"})
    available, reason = sandbox_prereq_status()
    assert available is False
    assert sandbox_runtime_available() is False
    assert "sunos5" in reason


# ── platform-aware error-message wording (linux vs darwin vs win32) ─────────


def test_unsandboxed_real_run_error_message_is_platform_aware_linux(monkeypatch):
    _patch_platform(monkeypatch, "linux")
    _patch_which(monkeypatch, present=set())
    msg = str(cd.UnsandboxedRealRunError())
    # Linux remediation: install bubblewrap + socat.
    assert "bubblewrap" in msg
    assert "socat" in msg
    assert "apt install" in msg
    # security framing is unchanged
    assert "no OS sandbox" in msg


def test_unsandboxed_real_run_error_message_is_platform_aware_darwin(monkeypatch):
    _patch_platform(monkeypatch, "darwin")
    _patch_which(monkeypatch, present=set())
    msg = str(cd.UnsandboxedRealRunError())
    # macOS: Seatbelt is built-in — a missing sandbox means it FAILED TO INITIALIZE,
    # not that an install is missing. So no apt-install hint, but a Seatbelt note.
    assert "Seatbelt" in msg
    assert "built-in" in msg.lower()
    assert "apt install" not in msg


def test_unsandboxed_real_run_error_message_is_platform_aware_win32(monkeypatch):
    _patch_platform(monkeypatch, "win32")
    _patch_which(monkeypatch, present=set())
    msg = str(cd.UnsandboxedRealRunError())
    assert "WSL2" in msg
    assert "apt install" not in msg


# ── back-compat: the old name still resolves to the new wrapper ─────────────


def test_bwrap_sandbox_wrap_alias_is_native_sandbox_wrap():
    """The historical ``bwrap_sandbox_wrap`` name MUST remain a working alias for
    ``native_sandbox_wrap`` so existing callers keep working after the rename."""
    assert bwrap_sandbox_wrap is native_sandbox_wrap
    assert cd.bwrap_sandbox_wrap is cd.native_sandbox_wrap
    # and it is still exported
    assert "bwrap_sandbox_wrap" in cd.__all__
    assert "native_sandbox_wrap" in cd.__all__


# ── the rename did NOT change wrap() output (byte-identical pin) ─────────────


def test_native_sandbox_wrap_output_unchanged(tmp_path):
    """``native_sandbox_wrap`` (and thus the alias) must emit the SAME
    ``--settings`` sandbox JSON as before the rename — the rename is naming +
    detection + messaging only, never a behavior change to the injected argv."""
    clone = tmp_path / "clone"
    clone.mkdir()
    clone_resolved = str(clone.resolve())

    argv_in = ["claude", "-p", "x", "--model", "haiku"]
    out = native_sandbox_wrap(str(clone))(list(argv_in))

    # original argv is preserved verbatim as a prefix; --settings appended LAST.
    assert out[: len(argv_in)] == argv_in
    assert out[len(argv_in)] == "--settings"
    assert len(out) == len(argv_in) + 2

    settings = json.loads(out[-1])
    # EXACT expected sandbox JSON (fail-closed + clone-confined + no net egress).
    assert settings == {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "filesystem": {"allowWrite": [clone_resolved]},
            "network": {"allowedDomains": []},
        }
    }

    # the alias produces byte-identical argv.
    out_alias = bwrap_sandbox_wrap(str(clone))(list(argv_in))
    assert out_alias == out
