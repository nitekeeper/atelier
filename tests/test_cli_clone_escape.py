"""R1 clone-escape guard — the HIGHEST-SEVERITY safety test for the CLI adapter.

``bypassPermissions`` removes the human gate; a prior real incident had a wave-1
teammate write into live ``~/apps/atelier``. The host MUST refuse to spawn any
``claude`` call whose resolved cwd / ``--add-dir`` escapes the experiment clone —
and it must refuse BEFORE the subprocess is launched (runner call-count 0).

These tests inject a :class:`FakeCliRunner` purely to PROVE it is never called:
the guard fires before the runner is reached.
"""

from __future__ import annotations

import asyncio

import pytest

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    CloneEscapeError,
    FakeCliRunner,
    run_attempt,
)
from scripts.result_journal import ResultJournal


def _task(task_id="t-1"):
    return {"task_id": task_id, "assigned_persona": "be-1", "phase": "tdd:green"}


def _run(coro):
    return asyncio.run(coro)


def _call(*, clone_dir, cwd=None, add_dir, runner):
    return run_attempt(
        _task(),
        1,
        budget=BudgetPool(total_tokens=100_000),
        journal=ResultJournal(),
        model="sonnet",
        briefing="b",
        clone_dir=clone_dir,
        runner=runner,
        cwd=cwd,
        add_dir=add_dir,
    )


def test_add_dir_dotdot_traversal_escape_refused_pre_spawn(tmp_path):
    """An --add-dir using ``..`` to climb out of the clone is REFUSED before any
    spawn — the runner is NEVER called."""
    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True)
    sibling = tmp_path / "live-apps"
    sibling.mkdir()
    runner = FakeCliRunner()

    # ../../live-apps resolves outside the clone.
    escape = clone / ".." / ".." / "live-apps"
    with pytest.raises(CloneEscapeError):
        _run(_call(clone_dir=str(clone), add_dir=str(escape), runner=runner))
    assert runner.call_count == 0


def test_add_dir_absolute_path_escape_refused_pre_spawn(tmp_path):
    """An absolute --add-dir pointing entirely outside the clone is refused."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runner = FakeCliRunner()

    with pytest.raises(CloneEscapeError):
        _run(_call(clone_dir=str(clone), add_dir=str(outside), runner=runner))
    assert runner.call_count == 0


def test_cwd_escape_refused_pre_spawn(tmp_path):
    """A cwd outside the clone (absolute escape) is refused before spawning."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside-cwd"
    outside.mkdir()
    runner = FakeCliRunner()

    with pytest.raises(CloneEscapeError):
        _run(_call(clone_dir=str(clone), cwd=str(outside), add_dir=str(clone), runner=runner))
    assert runner.call_count == 0


def test_cwd_dotdot_escape_refused_pre_spawn(tmp_path):
    """A cwd using ``..`` to climb out of the clone is refused before spawning."""
    clone = tmp_path / "a" / "clone"
    clone.mkdir(parents=True)
    runner = FakeCliRunner()

    escape_cwd = clone / ".." / ".."  # climbs to tmp_path, outside the clone
    with pytest.raises(CloneEscapeError):
        _run(
            _call(
                clone_dir=str(clone),
                cwd=str(escape_cwd),
                add_dir=str(clone),
                runner=runner,
            )
        )
    assert runner.call_count == 0


def test_inside_clone_is_allowed(tmp_path):
    """A cwd / --add-dir that resolves INSIDE the clone (or the clone itself) is
    allowed — the guard does not over-refuse legitimate sub-paths."""
    clone = tmp_path / "clone"
    sub = clone / "src"
    sub.mkdir(parents=True)
    runner = FakeCliRunner(
        structured_output={
            "type": "task_result",
            "task_id": "t-1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "f", "sha": "s"}],
            "notes_md": "ok",
        }
    )
    env = _run(_call(clone_dir=str(clone), cwd=str(sub), add_dir=str(clone), runner=runner))
    assert env["status"] == "done"
    assert runner.call_count == 1  # allowed → the runner WAS reached


def test_dotdot_that_stays_inside_is_allowed(tmp_path):
    """A ``..`` path that NORMALIZES back to inside the clone is allowed (the
    guard resolves before checking, so harmless round-trips pass)."""
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    runner = FakeCliRunner(
        structured_output={
            "type": "task_result",
            "task_id": "t-1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "f", "sha": "s"}],
            "notes_md": "ok",
        }
    )
    # clone/src/.. == clone — still inside.
    inside_roundtrip = clone / "src" / ".."
    env = _run(_call(clone_dir=str(clone), add_dir=str(inside_roundtrip), runner=runner))
    assert env["status"] == "done"
    assert runner.call_count == 1
