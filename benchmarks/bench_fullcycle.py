#!/usr/bin/env python3
"""Full-cycle orchestrator-level A/B for the terse/"caveman" lever (B1).

Answers "keep or kill caveman?" — which a per-worker benchmark cannot, because
terse's STATED purpose is orchestrator-context economy (compact worker notes_md ->
smaller wave digest the orchestrator carries downstream), not worker economy.

This is the harness that produced the kill verdict: B1 has since been DELETED from
production. The ``--live`` arm below toggles ``ATELIER_INCLUDE_TERSE`` — an env that
no longer does anything in production — so a live re-run now measures bare-vs-bare
(a useful zero-delta control). The deterministic ``cap-demo`` still bounds B1's
theoretical benefit from atelier's LIVE digest constants (B2 + the 200-char cap).

Two modes:

  (default) `cap-demo` — DETERMINISTIC, no API spend. Proves how much terse-ing a
    worker's notes_md actually shrinks the downstream wave DIGEST, given the digest
    already head/tail-caps each worker's notes to _DIGEST_NOTES_CAP (200) chars and
    (B2) compresses it. This bounds B1's BENEFIT.

  --live  — REAL A/B. Drives atelier's run_host_pipeline_for_project on a fixture
    DAG with ATELIER_INCLUDE_TERSE=1 vs =0, n reps, capturing the ROOT BudgetPool
    four-channel total (the whole-run cost metric). With B1 deleted this is a
    zero-delta control; it remains as the keep/kill harness of record.
    NOTE: a real multi-worker cycle is expensive + the host engine has known
    worktree flakiness — run deliberately.

The interpretation: B1 is net-positive only if (digest-tokens saved across all
workers x review rounds) > (extra worker deliberation cost). The cap-demo bounds
the numerator; the per-worker benchmark (run.py) measured the denominator (+99%
cost on haiku). --live settled it end-to-end (+37.8% total tokens even on sonnet).

Usage:
  python3 bench_fullcycle.py              # deterministic cap-demo + verdict
  python3 bench_fullcycle.py --live --reps 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO_ROOT = BENCH.parent  # atelier repo root (benchmarks/ lives at the top level)
sys.path.insert(0, str(REPO_ROOT))


def cap_demo() -> None:
    """Deterministic: how much does terse-ing a worker shrink the wave digest?"""
    from scripts.pm_dispatch import _DIGEST_NOTES_CAP, default_wave_digest

    print(f"_DIGEST_NOTES_CAP = {_DIGEST_NOTES_CAP} chars/worker (head/tail preview bound)\n")
    rows = []
    for n_workers in (1, 3, 5):
        verbose = [
            {
                "task_id": f"AI-{i}",
                "status": "done",
                "artifacts": [{"path": f"f{i}.py", "sha": "s"}],
                "notes_md": "Implemented the change with rationale. " * 80,
            }
            for i in range(n_workers)
        ]
        terse = [
            {
                "task_id": f"AI-{i}",
                "status": "done",
                "artifacts": [{"path": f"f{i}.py", "sha": "s"}],
                "notes_md": "done: added guard.",
            }
            for i in range(n_workers)
        ]
        dv = len(default_wave_digest(verbose))
        dt = len(default_wave_digest(terse))
        rows.append((n_workers, dv, dt, dv - dt))
        print(
            f"  {n_workers} workers: digest verbose={dv}c terse={dt}c  -> B1 saves ~{dv - dt}c "
            f"(~{(dv - dt) // 4} tokens) downstream"
        )
    saved_5 = rows[-1][3]
    print(
        "\nVERDICT (structural):\n"
        f"  B1 (terse instruction) BENEFIT is bounded by the {_DIGEST_NOTES_CAP}-char digest cap +\n"
        f"  the B2 codec: ~{saved_5 // 4} downstream tokens saved even for a 5-worker wave.\n"
        "  B1 COST (per run.py): +99% worker cost & worse correctness on haiku\n"
        "  (~neutral on sonnet). Tiny capped benefit << large worker cost => B1 is a NET LOSS,\n"
        "  worst on the cheap tier. KEEP B2 (free post-hoc codec) + the cap; B1 is now DELETED.\n"
        "  (--live confirms the whole-cycle delta against the live engine.)"
    )


def live_ab(reps: int) -> None:
    """REAL A/B: run a full host cycle with ATELIER_INCLUDE_TERSE on vs off and
    compare the root BudgetPool four-channel total. Expensive + host-engine-flaky."""
    import os
    import shutil
    import subprocess
    import tempfile

    from scripts.budget_pool import BudgetPool, format_usage_report
    from scripts.cli_dispatch import native_sandbox_wrap
    from scripts.host_scheduler import run_host_pipeline_for_project
    from scripts.result_journal import ResultJournal

    fixture = BENCH / "fixtures" / "widget-app"
    # A minimal 2-task implementation DAG (sonnet tier so terse is NOT haiku-gated —
    # this isolates B1's whole-cycle effect where the gate does not pre-empt it).
    tasks = [
        {
            "task_id": "AI-1",
            "parallel_group": 0,
            "assigned_persona": "backend-engineer-1",
            "phase": "tdd:green",
            "writes": ["pyutil/text.py"],
            "reads": [],
            "description": "Implement slugify(title) in pyutil/text.py.",
        },
        {
            "task_id": "AI-2",
            "parallel_group": 0,
            "assigned_persona": "backend-engineer-1",
            "phase": "tdd:green",
            "writes": ["pyutil/files.py"],
            "reads": [],
            "description": "Implement safe_join(base_dir, user_filename) in pyutil/files.py.",
        },
    ]
    # Capture the engine's OWN cost report — it reads the CHARGED eff_budget
    # (the passed `budget` stays zero in a non-neutral run mode). The report is
    # logged at INFO by logging.getLogger("scripts.host_scheduler"); grab it there.
    import asyncio
    import logging
    import re

    class _Cap(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.last = ""

        def emit(self, rec: logging.LogRecord) -> None:
            m = rec.getMessage()
            if "cost report (tokens):" in m:
                self.last = m

    cap = _Cap()
    hs_log = logging.getLogger("scripts.host_scheduler")
    hs_log.addHandler(cap)
    hs_log.setLevel(logging.INFO)

    def _total_from_report() -> int:
        nums = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", cap.last)}
        return nums.get("total", 0)

    results: dict[str, list[int]] = {"on": [], "off": []}
    for arm, env_val in (("on", "1"), ("off", "0")):
        for r in range(reps):
            wd = Path(tempfile.mkdtemp(prefix=f"fc-{arm}-{r}-"))
            shutil.copytree(fixture, wd / "repo", dirs_exist_ok=True)
            repo = wd / "repo"
            shutil.rmtree(repo / ".git", ignore_errors=True)
            for a in (["init"], ["add", "-A"], ["commit", "-m", "base"]):
                subprocess.run(
                    ["git", "-c", "user.email=b@b", "-c", "user.name=b", *a],
                    cwd=repo,
                    capture_output=True,
                    check=False,
                )
            budget = BudgetPool(total_tokens=10_000_000)
            env = {**os.environ, "ATELIER_INCLUDE_TERSE": env_val}
            cap.last = ""
            try:
                asyncio.run(
                    run_host_pipeline_for_project(
                        tasks,
                        clone_dir=str(repo),
                        budget=budget,
                        journal=ResultJournal(),
                        env=env,
                        sandbox_wrap=native_sandbox_wrap(str(repo)),
                    )
                )
                total = _total_from_report()  # from eff_budget, via the engine's report
                if total > 0:
                    results[arm].append(total)
                print(f"  terse={arm} rep{r}: {cap.last or '(no cost report emitted)'}")
            except Exception as e:
                print(f"  terse={arm} rep{r}: ERROR {type(e).__name__}: {str(e)[:140]}")
            finally:
                shutil.rmtree(wd, ignore_errors=True)
    if not results["on"] or not results["off"]:
        print(
            f"\nWHOLE-CYCLE: insufficient data (on={results['on']} off={results['off']}) — "
            "host-engine flakiness; re-run or raise reps."
        )
        return
    on = sum(results["on"]) / len(results["on"])
    off = sum(results["off"]) / len(results["off"])
    print(
        f"\nWHOLE-CYCLE four-channel total (eff_budget): terse-ON={on:.0f} vs terse-OFF={off:.0f} "
        f"({(on - off) / off * 100:+.1f}% for keeping terse)"
    )
    _ = format_usage_report  # (kept for the per-arm report import; totals parsed from the log)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="run the real (expensive) A/B")
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args(argv)
    cap_demo()
    if a.live:
        print("\n=== LIVE A/B (real host cycles) ===")
        live_ab(a.reps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
