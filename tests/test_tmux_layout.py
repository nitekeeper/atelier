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
