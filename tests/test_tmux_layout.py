"""Tests for scripts/tmux_layout.py — pure compute_layout invariants + apply gate (atelier#63)."""

import pytest

from scripts import preflight, tmux_layout
from scripts.pm_dispatch import MAX_PARALLEL_WORKERS

_TERM_W = 240
_TERM_H = 60


@pytest.mark.parametrize(
    "n_workers",
    [0, 1, 3, 5, MAX_PARALLEL_WORKERS, MAX_PARALLEL_WORKERS + 1],
)
def test_compute_layout_invariants(n_workers):
    rects = tmux_layout.compute_layout(n_workers, _TERM_W, _TERM_H)
    expected_workers = min(n_workers, MAX_PARALLEL_WORKERS)
    # Length == clamp(n, MAX) + 1 (PM + clamped workers).
    assert len(rects) == expected_workers + 1
    # rect[0] is the PM pane: left third, full height.
    pm = rects[0]
    assert pm.x == 0
    assert pm.y == 0
    assert pm.width == _TERM_W // 3
    assert pm.height == _TERM_H
    pm_width = _TERM_W // 3
    # Every worker rect starts at or after pm_width, with strictly positive dims.
    for worker in rects[1:]:
        assert worker.x >= pm_width
        assert worker.width > 0
        assert worker.height > 0
        assert worker.x >= 0
        assert worker.y >= 0


def test_compute_layout_n_zero_is_pm_only():
    rects = tmux_layout.compute_layout(0, _TERM_W, _TERM_H)
    assert len(rects) == 1
    assert rects[0].x == 0


def test_compute_layout_clamps_at_max():
    over = tmux_layout.compute_layout(MAX_PARALLEL_WORKERS + 1, _TERM_W, _TERM_H)
    at_max = tmux_layout.compute_layout(MAX_PARALLEL_WORKERS, _TERM_W, _TERM_H)
    assert len(over) == len(at_max) == MAX_PARALLEL_WORKERS + 1


def test_compute_layout_deterministic():
    a = tmux_layout.compute_layout(3, _TERM_W, _TERM_H)
    b = tmux_layout.compute_layout(3, _TERM_W, _TERM_H)
    assert a == b


def test_compute_layout_negative_raises():
    with pytest.raises(ValueError):
        tmux_layout.compute_layout(-1, _TERM_W, _TERM_H)


def test_compute_layout_nonpositive_dims_raise():
    with pytest.raises(ValueError):
        tmux_layout.compute_layout(1, 0, _TERM_H)
    with pytest.raises(ValueError):
        tmux_layout.compute_layout(1, _TERM_W, 0)


def test_compute_layout_no_negative_or_zero_dims():
    rects = tmux_layout.compute_layout(MAX_PARALLEL_WORKERS, _TERM_W, _TERM_H)
    for r in rects:
        assert r.width > 0
        assert r.height > 0
        assert r.x >= 0
        assert r.y >= 0


# ── apply_layout no-op gate (the only branch unit-tested for the impure shim) ─


def test_apply_layout_wrong_mode_returns_false(monkeypatch):
    # Even with tmux "available", a non-agent-team mode no-ops before any call.
    called = []
    monkeypatch.setattr(preflight, "tmux_available", lambda: called.append("tmux") or True)
    assert tmux_layout.apply_layout(3, mode="subagent") is False
    # The mode gate short-circuits BEFORE tmux_available() is consulted: the
    # mode != agent-team check is the left operand of the OR, so tmux is never
    # probed (0 calls). This pins the "no-ops before any call" property.
    assert called == []


def test_apply_layout_no_tmux_returns_false(monkeypatch):
    monkeypatch.setattr(preflight, "tmux_available", lambda: False)
    assert tmux_layout.apply_layout(3, mode="agent-team") is False
    assert tmux_layout.apply_layout(3) is False


# ── @desired_title pane-label setter (atelier#79) ───────────────────────────


class _OkProc:
    """Minimal stand-in for subprocess.CompletedProcess (returncode 0)."""

    returncode = 0


def _fake_tmux(monkeypatch, sink):
    """tmux available + a fake subprocess.run that records argv and succeeds."""
    monkeypatch.setattr(preflight, "tmux_available", lambda: True)
    monkeypatch.setattr(preflight, "get_tmux_cmd", lambda: ["tmux"])

    def fake_run(argv, **kwargs):
        sink.append(argv)
        return _OkProc()

    monkeypatch.setattr(tmux_layout.subprocess, "run", fake_run)


def test_set_pane_title_writes_desired_title_user_option(monkeypatch):
    """set_pane_title routes the label via ``set-option -p @desired_title``."""
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    assert tmux_layout.set_pane_title("%3", "[w2] backend-engineer-1", mode="agent-team") is True
    assert len(calls) == 1
    argv = calls[0]
    assert "set-option" in argv and "-p" in argv
    assert argv[argv.index("-t") + 1] == "%3"
    assert argv[argv.index("@desired_title") + 1] == "[w2] backend-engineer-1"
    # Routed via the user-option, NOT select-pane -T (atelier#79 AC fidelity):
    # OSC-2 would clobber select-pane -T on the next turn.
    assert "select-pane" not in argv
    assert "-T" not in argv


def test_set_pane_title_sanitizes_hash_and_control_chars(monkeypatch):
    """``#`` is doubled (never read as a ``#{...}`` token) and ctrl chars stripped."""
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    tmux_layout.set_pane_title("%1", "wave #3\nX", mode="agent-team")
    argv = calls[0]
    assert argv[argv.index("@desired_title") + 1] == "wave ##3X"


def test_set_pane_title_no_tmux_returns_false(monkeypatch):
    monkeypatch.setattr(preflight, "tmux_available", lambda: False)
    called: list[int] = []
    monkeypatch.setattr(tmux_layout.subprocess, "run", lambda *a, **k: called.append(1))
    assert tmux_layout.set_pane_title("%1", "x", mode="agent-team") is False
    assert called == []  # gate short-circuits before any tmux call


def test_set_pane_title_wrong_mode_returns_false(monkeypatch):
    monkeypatch.setattr(preflight, "tmux_available", lambda: True)
    called: list[int] = []
    monkeypatch.setattr(tmux_layout.subprocess, "run", lambda *a, **k: called.append(1))
    assert tmux_layout.set_pane_title("%1", "x", mode="subagent") is False
    assert called == []


def test_set_pane_title_soft_fails_on_subprocess_error(monkeypatch):
    """A tmux failure is swallowed — the setter never raises (returns False)."""
    monkeypatch.setattr(preflight, "tmux_available", lambda: True)
    monkeypatch.setattr(preflight, "get_tmux_cmd", lambda: ["tmux"])

    def boom(*a, **k):
        raise OSError("no tmux server")

    monkeypatch.setattr(tmux_layout.subprocess, "run", boom)
    assert tmux_layout.set_pane_title("%1", "x", mode="agent-team") is False


def test_set_pane_titles_bulk_best_effort(monkeypatch):
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    n = tmux_layout.set_pane_titles({"%1": "a", "%2": "b"}, mode="agent-team")
    assert n == 2
    targeted = {c[c.index("-t") + 1] for c in calls}
    assert targeted == {"%1", "%2"}


def test_apply_layout_composes_labels_after_geometry(monkeypatch):
    """apply_layout sets @desired_title labels right after realizing the geometry."""
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    ok = tmux_layout.apply_layout(2, mode="agent-team", labels={"%0": "PM", "%1": "[w1] sdet-1"})
    assert ok is True
    assert any("select-layout" in c for c in calls)
    label_writes = [c for c in calls if "@desired_title" in c]
    assert len(label_writes) == 2
