"""Atelier agent-team pane layout — pure geometry + a thin tmux apply shim (atelier#63).

The PM pane is fixed at the left 1/3 of the terminal; the worker panes tile the
right 2/3 in a 2-D grid. :func:`compute_layout` is PURE (no tmux calls, fully
unit-testable); :func:`apply_layout` is a thin, best-effort IMPURE shim that
drives tmux to realize the computed layout in the current window.

Design facts (atelier design spec §9.3): index 0 of the returned list is ALWAYS
the PM rect; worker count is clamped to
:data:`scripts.pm_dispatch.MAX_PARALLEL_WORKERS`.
"""

from __future__ import annotations

import math
import subprocess
import sys
from typing import NamedTuple

from scripts import preflight
from scripts.dispatch import DISPATCH_MODE_AGENT_TEAM
from scripts.pm_dispatch import MAX_PARALLEL_WORKERS


class PaneRect(NamedTuple):
    """An axis-aligned pane rectangle in terminal-cell coordinates.

    ``x``/``y`` are the top-left origin; ``width``/``height`` are extents. All
    four are non-negative integers; for positive terminal dimensions every rect
    :func:`compute_layout` returns has strictly positive ``width``/``height``.
    """

    x: int
    y: int
    width: int
    height: int


def compute_layout(n_workers: int, term_width: int, term_height: int) -> list[PaneRect]:
    """Compute pane rectangles for the PM + ``n_workers`` worker panes. PURE.

    Layout:
      * PM pane = left third — ``x=0, y=0, width=term_width // 3,
        height=term_height``. ALWAYS index 0 of the result.
      * Workers tile the right region (``x`` starting at ``pm_width``,
        ``width = term_width - pm_width``) in a 2-D grid:
        ``cols = ceil(sqrt(k))``, ``rows = ceil(k / cols)`` for
        ``k = min(n_workers, MAX_PARALLEL_WORKERS)``.

    Returns ``[PM_rect, worker_rect_1, ..., worker_rect_k]`` of length
    ``min(n_workers, MAX_PARALLEL_WORKERS) + 1``. For ``n_workers == 0`` the
    result is just ``[PM_rect]`` (length 1).

    Raises:
        ValueError if ``n_workers < 0`` or either terminal dimension <= 0.
    """
    if n_workers < 0:
        raise ValueError(f"n_workers must be >= 0, got {n_workers}")
    if term_width <= 0 or term_height <= 0:
        raise ValueError(f"terminal dimensions must be positive, got {term_width}x{term_height}")

    pm_width = term_width // 3
    if pm_width <= 0:
        raise ValueError(f"term_width {term_width} too small to allocate a positive PM pane width")
    pm_rect = PaneRect(x=0, y=0, width=pm_width, height=term_height)
    rects: list[PaneRect] = [pm_rect]

    k = min(n_workers, MAX_PARALLEL_WORKERS)
    if k == 0:
        return rects

    right_x = pm_width
    right_width = term_width - pm_width
    if right_width <= 0:
        raise ValueError(f"term_width {term_width} too small to allocate a positive worker region")

    cols = math.ceil(math.sqrt(k))
    rows = math.ceil(k / cols)

    cell_width = right_width // cols
    cell_height = term_height // rows
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError(
            f"terminal {term_width}x{term_height} too small to tile {k} worker pane(s)"
        )

    for i in range(k):
        row = i // cols
        col = i % cols
        rects.append(
            PaneRect(
                x=right_x + col * cell_width,
                y=row * cell_height,
                width=cell_width,
                height=cell_height,
            )
        )
    return rects


def apply_layout(n_workers: int, *, mode: str | None = None) -> bool:
    """Best-effort: drive tmux to realize the computed layout in the CURRENT window.

    GATE (no-op fallback): if ``mode`` is provided and != ``"agent-team"``, OR
    tmux is unavailable, returns ``False`` without touching tmux.

    Thin by design — the testable geometry lives in :func:`compute_layout`. This
    shim NEVER raises on a tmux failure: it logs to stderr and returns ``False``
    so a layout hiccup can never crash a cycle.
    """
    if (mode is not None and mode != DISPATCH_MODE_AGENT_TEAM) or not preflight.tmux_available():
        return False
    try:
        # main-vertical with a left-anchored PM pane is the closest built-in
        # tmux tiling to the design's "PM left 1/3, workers tile the right".
        subprocess.run(
            [*preflight.get_tmux_cmd(), "select-layout", "main-vertical"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"tmux_layout: apply_layout best-effort no-op ({exc})\n")
        return False
    return True


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="tmux_layout",
        description="Compute or apply the atelier agent-team pane layout.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    compute = sub.add_parser("layout:compute", help="Print the computed pane rects.")
    compute.add_argument("n_workers", type=int)
    compute.add_argument("--width", type=int, default=240)
    compute.add_argument("--height", type=int, default=60)

    apply_p = sub.add_parser("layout:apply", help="Apply the layout to the current window.")
    apply_p.add_argument("n_workers", type=int)
    apply_p.add_argument("--mode", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "layout:compute":
        for rect in compute_layout(args.n_workers, args.width, args.height):
            sys.stdout.write(f"{rect}\n")
        return 0
    if args.cmd == "layout:apply":
        ok = apply_layout(args.n_workers, mode=args.mode)
        sys.stdout.write(f"tmux_layout: applied={ok}\n")
        return 0
    parser.error(f"unknown command {args.cmd!r}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
