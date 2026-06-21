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

## Glossary — what every term means

### Arms & levers (the thing each cell is testing)

A **lever** is a prompt rule appended to the agent's briefing. An **arm** is one
configuration of levers; the benchmark runs all four arms on the same task and compares
them. Everything is measured **relative to `bare`** — bare is the honest "real agent, no
rule" baseline, so any difference is the lever's effect, not the model being chatty.

- **`bare`** — the task only, **no lever**. The baseline; the `Δ vs bare` columns subtract
  this.
- **`terse`** — task + the terse / "caveman" *briefing rule* (**B1**): an instruction
  telling the worker to write its free-text prose compactly ("talk like a smart caveman").
  An **input-side** lever (it shrinks what the agent *says*). **Deleted from production**
  as a measured net loss; kept here only as a frozen constant so the A/B reproduces.
- **`minimal_diff`** — task + the minimal-diff rule (`_MINIMAL_DIFF_RULE`): a "build the
  smallest thing that works" laziness ladder (YAGNI → stdlib → native feature → one line)
  with an anti-deliberation reflex and a safety carve-out. An **output-side** lever (it
  shrinks what the agent *builds*). **Live in production**, phase-gated to implementer
  phases. Adapted from [ponytail](https://github.com/DietrichGebert/ponytail).
- **`both`** — task + terse + minimal_diff together.

> **B1 vs B2 — don't conflate them.** "Caveman" names two different mechanisms. **B1** is
> the terse *briefing rule above* (tells the agent to be brief) — **removed**. **B2** is
> the `caveman_codec.py` *post-processor* that deterministically strips filler from text
> *after* it is written, never touching protected tokens — **kept** (it is free; no agent
> deliberation cost). This benchmark only ever tested B1.

### Metric columns (what each number in the result tables means)

- **`task` / `arm` / `model`** — which task, which lever-arm, and which model
  (`haiku` = claude-haiku-4-5, `sonnet` = claude-sonnet-4-6, `opus` = claude-opus-4-8) the
  cell ran.
- **`n`** — how many cells (reps × …) were averaged into that row.
- **`LOC` (loc_median)** — **L**ines **O**f **C**ode the agent *added*, from `git diff`
  added-line count (lockfiles/minified excluded). The over-build proxy — the `+N` a PR
  shows. **Lower = less built.** Only meaningful read alongside `correct` (fewer lines
  that also do less is *less*, not *less-bloated*).
- **`tokens` (tokens_mean)** — total tokens for the cell, four-channel
  (output + input + cache_creation + cache_read), straight from the `claude` CLI JSON.
  Cache-noisy — treat as secondary to cost/wall.
- **`cost $` (cost_mean)** — US-dollar cost of the cell, from the CLI's `total_cost_usd`.
  One of the two clean economic signals.
- **`wall s` (wall_ms_mean)** — **wall-clock** seconds, real elapsed time from launching
  `claude -p` to its exit (captured in ms, shown in s). The latency signal; catches a
  lever that makes the model *deliberate longer* even when output isn't bigger.
- **`over_eng` (over_eng_mean)** — **over-engineering** score from an LLM judge
  (`claude-sonnet-4-6`, temp 0, published rubric), **0–3**: 0 = minimal/idiomatic,
  1 = slightly more than needed, 2 = notable extra abstraction/deps/config,
  3 = framework-for-a-one-off. **Lower = better.** The judge must name the specific
  over-built construct.
- **`correct` (correct_mean)** — correctness score from the same LLM judge, **0–3**:
  0 = doesn't satisfy the ticket, 1 = partial, 2 = works on the happy path,
  3 = correct incl. edge cases. **Higher = better.** Read against LOC to catch "won the
  LOC metric by doing less."
- **`safe` (safe_rate)** — fraction of safety-task cells that survived the adversarial
  input. Deterministic, stdlib-only: the produced `safe_join` is executed against
  `../../../../etc/passwd`; 1.00 = contained (safe), lower = some cell let the path escape.
  `-` means the task had no safety axis.

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

Per-run writeups are **generated locally** into `results/` and are **gitignored** (like
`runs/`) — they analyze throwaway, cache-noisy runs and don't belong under version
control. Re-run the harness to regenerate them. The **stable conclusions** the runs
support live here:

### Stable findings (2026-06-20)

- **Terse rule (B1) — removed.** The terse / "caveman" *briefing instruction* was a
  measured net loss, decided on the **whole-cycle A/B** (+37.8% total tokens on sonnet,
  deliberation compounding across turns) and a deterministic capped-benefit proof. (A
  later per-tier per-worker sweep had terse ≈neutral — an honest negative; the kill never
  rested on the per-worker axis.)
- **`minimal_diff` — kept, phase-gated to implementers.** The cost / over-engineering
  winner at every tier incl. opus (−16…−27% cost, over-engineering → 0). It **loses only
  on compound, multi-requirement tasks**, where "build the minimum" can drop a stated
  requirement (the `form-validation` case). A sharpened prompt clause did **not**
  measurably fix that at n=2.

### The decision unit is mechanism × context, not whole levers

Each lever wins in some parts and loses in others; route each *mechanism* to the *context*
where it helps. Proven once already: "caveman" was two mechanisms with opposite verdicts —
**B1** (terse instruction, removed) vs **B2** (the post-hoc `caveman_codec` filler-strip,
kept, free). The same discipline applies to `minimal_diff`:

| mechanism | context | verdict | policy |
|---|---|---|---|
| B2 codec | any orchestrator-facing prose | win (free) | keep, always |
| B1 terse instruction | whole cycle, every tier | loss | removed; if revisited, gate **off for haiku**, not globally |
| minimal_diff ladder | over-build-prone / single-requirement impl | win | keep ON |
| minimal_diff ladder | **compound / multi-requirement** | loss (drops a requirement) | **gate, don't preach** ↓ |

**Open follow-up:** gate `minimal_diff` on compound tasks *deterministically* —
`compose_briefing` already receives `acceptance_criteria`, so "compound" is
`len(acceptance_criteria) > 1`. On that signal, soften/suppress the ladder or attach a
per-criterion completeness check in the review loop. A mechanism on a signal we already
have — not a prompt nudge.
