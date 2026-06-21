# Atelier token-lever benchmark

A measurement harness for atelier's prompt-side token levers, built on
[ponytail](https://github.com/DietrichGebert/ponytail)'s agentic methodology: every
cell is a **real headless `claude -p` session** editing an **isolated copy of a seeded
fixture repo**, scored on the `git diff` it leaves behind — not a single-shot
completion. It exists to answer keep-or-kill questions about atelier's own rule
constants with data, not intuition.

It is the harness that produced the **kill verdict for the terse / "caveman" briefing
rule (B1)**, which has since been deleted from production (see [`results/`](results/)).

## Arms

| arm | injects | source |
|---|---|---|
| `bare` | nothing (task only) | the honest baseline |
| `terse` | the terse/"caveman" output rule | `_FROZEN_TERSE_RULE` in `run.py` — a frozen copy of the **deleted** B1 text, kept so the A/B stays reproducible |
| `minimal_diff` | atelier's `_MINIMAL_DIFF_RULE` | imported **live** from `scripts.dispatch` |
| `both` | terse + minimal_diff | — |

The benchmark imports nothing terse from atelier (B1 is gone from production); only
`_MINIMAL_DIFF_RULE` is a live import, so the `minimal_diff`/`both` arms always test the
*current* production rule.

## Two harnesses

- **`run.py`** — the per-worker matrix: tasks × arms × models × reps, each an isolated
  `claude -p` cell scored on git-diff LOC, four-channel tokens, cost, wall-time, a
  deterministic adversarial safety check (safe-join), and an LLM over-engineering /
  correctness judge.
- **`bench_fullcycle.py`** — the orchestrator-level A/B: a deterministic `cap-demo` that
  bounds B1's theoretical benefit from atelier's live digest constants, plus a `--live`
  mode that drives atelier's real `run_host_pipeline_for_project` with the lever on vs
  off and reads the whole-run cost report.

## Reproduce

Needs the `claude` CLI (this is the harness — no SDK), Python 3.11+, and an
authenticated Claude Code. Run from the atelier repo root (the harness puts the repo on
`sys.path` so it imports `scripts.*` directly).

```bash
# Prove the instruments with NO API spend (this is the CI gate):
python benchmarks/run.py --selftest-offline

# Prove the instruments INCLUDING the LLM judge (small spend):
python benchmarks/run.py --selftest

# Run the per-worker matrix (spends — isolated claude -p per cell):
python benchmarks/run.py --reps 2 --models haiku,sonnet,opus

# Narrow it:
python benchmarks/run.py --arms bare,minimal_diff --tasks safe-join --models haiku

# Orchestrator-level A/B:
python benchmarks/bench_fullcycle.py            # deterministic cap-demo, no API
python benchmarks/bench_fullcycle.py --live --reps 2   # real host cycles (expensive)
```

Each cell runs `bypassPermissions` with `--disallowedTools Bash WebFetch WebSearch`
(agents only edit files — no server, DB, or network) in a fresh fixture copy under a
tempdir, which is removed after scoring. Aggregate results are written to
`benchmarks/runs/<stamp>.json` (gitignored).

## CI

CI runs only `run.py --selftest-offline` — it validates the deterministic instruments
(the adversarial safety scorer discriminates safe vs unsafe; the four arms wire the
right rule text) with **zero API spend**. The paid matrix is never run in CI, exactly
like ponytail's gate. The harness is also linted (`ruff`) and formatted with the rest of
the repo.

## Metrics

- **loc** (git-diff added lines): the over-build proxy — the `+N` a PR shows.
- **safe** (gate, deterministic, stdlib-only): the produced `safe_join` is executed
  against `../../../../etc/passwd`; True = contained, False = escaped.
- **tokens** (four-channel: output + input + cache_creation + cache_read), **cost_usd**,
  **wall_ms**: straight from the `claude` CLI JSON.
- **over_engineering / correctness** (LLM judge, `claude-sonnet-4-6`, temp 0, published
  rubric): each scored 0–3.

Every instrument is proven before any paid cell runs: `--selftest-offline` checks the
deterministic scorer + arm wiring with no API, and `--selftest` additionally requires the
judge to rank a minimal diff strictly below a bloated one. If the instruments don't
discriminate, the harness refuses to spend.

## What this can and cannot show

- It **can** show whether a prompt-side lever cuts code/cost/tokens *without* dropping
  safety or correctness, on real multi-file edits, across model sizes, with variance.
- It **cannot** claim production-readiness from a handful of tasks; the deterministic
  safety check is a floor, not a proof of security; and four-channel tokens are
  cache-noisy (cost and wall-time are the cleaner economic signals).
- Small/uneven n on the live runs → the exact percentages are directional; the terse
  net-loss signal is robust because it triangulates three independent measurements.

## Results

The terse rule's kill verdict, triangulated three ways (2026-06-20):

- [`results/2026-06-20-per-worker.md`](results/2026-06-20-per-worker.md) — the per-worker
  matrix (pilot).
- [`results/2026-06-20-fair-test.md`](results/2026-06-20-fair-test.md) — over-build-prone
  tasks across haiku & sonnet (the decisive per-worker run).
- [`results/2026-06-20-whole-cycle-ab.md`](results/2026-06-20-whole-cycle-ab.md) — the
  live whole-cycle A/B (the keep-or-kill number).

Post-removal per-tier sweep:

- [`results/2026-06-20-three-model-matrix.md`](results/2026-06-20-three-model-matrix.md) —
  the full per-worker matrix across **haiku, sonnet, and opus** (120 cells). `minimal_diff`
  wins at every tier incl. opus; terse came out ≈neutral per-worker (it did *not* reproduce
  the +99%-on-haiku figure — variance/task-mix, an honest negative the kill never rested on).
