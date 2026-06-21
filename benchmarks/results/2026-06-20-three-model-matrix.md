# Atelier token-lever benchmark — three-model per-worker matrix (2026-06-20)

The full per-worker matrix across **haiku, sonnet, and opus**, after the terse rule
(B1) was removed from production. Run with the opus tier wired into the harness
(`MODELS["opus"] = "claude-opus-4-8"`).

```
python benchmarks/run.py --reps 2 --models haiku,sonnet,opus --stamp full-3model
```

5 tasks (date-field, slugify, safe-join, file-dropzone, form-validation) × 4 arms
(bare, terse, minimal_diff, both) × 3 models × 2 reps = **120 cells, 120 completed,
0 errors**. Cell spend ≈ $13.49 (plus ~120 sonnet judge calls). The terse arm uses the
frozen `_FROZEN_TERSE_RULE` (byte-identical to the deleted B1 constant); `minimal_diff`
is imported live from `scripts.dispatch`.

## Results — rolled across all 5 tasks (n=10 per model×arm)

| model | arm | LOC (median) | cost $ (mean) | wall s | over_eng | correct | safe_rate |
|---|---|--:|--:|--:|--:|--:|--:|
| **haiku** | bare | 10 | 0.0764 | 29.0 | 0.20 | 2.80 | 1.00 |
| | terse | 13 | 0.0662 | 21.5 | 0.20 | 2.60 | 1.00 |
| | minimal_diff | 8 | 0.0557 | 20.5 | 0.10 | 2.50 | 1.00 |
| | both | 12 | 0.0629 | 23.3 | 0.20 | 2.70 | 1.00 |
| **sonnet** | bare | 6 | 0.1009 | 13.0 | 0.00 | 2.70 | 1.00 |
| | terse | 5 | 0.0934 | 13.0 | 0.00 | 2.90 | 1.00 |
| | minimal_diff | 3 | 0.0846 | 13.0 | 0.00 | 2.60 | 1.00 |
| | both | 5 | 0.0869 | 12.5 | 0.00 | 2.80 | 1.00 |
| **opus** | bare | 10 | 0.1975 | 20.4 | 0.20 | 3.00 | 1.00 |
| | terse | 10 | 0.1994 | 19.5 | 0.00 | 3.00 | 1.00 |
| | minimal_diff | 1 | 0.1547 | 14.4 | 0.00 | 2.60 | 1.00 |
| | both | 6 | 0.1704 | 20.9 | 0.10 | 2.80 | 1.00 |

**Δ vs bare (cost / wall / LOC / correctness):**

| arm | haiku | sonnet | opus |
|---|---|---|---|
| terse | −13% / −26% / +2 / −0.20 | −7% / 0% / −0 / +0.20 | **+1% / −4% / +0 / 0.00** |
| minimal_diff | −27% / −29% / −2 / −0.30 | −16% / 0% / −2 / −0.10 | **−22% / −30% / −9 / −0.40** |
| both | −18% / −20% / +2 / −0.10 | −14% / −4% / −0 / +0.10 | −14% / +2% / −4 / −0.20 |

## Reading

**`minimal_diff` is the consistent winner — and it holds at the opus tier.** Cheaper at
every level (haiku −27%, sonnet −16%, opus −22%), drives the over-engineering judge to
~0, and cuts LOC hardest on opus (median 10 → 1). The cost is a correctness dip (−0.1 to
−0.4, worst on opus −0.40) — the completeness tradeoff the earlier runs already flagged,
now visible at the top tier too. This re-confirms keeping `minimal_diff` (phase-gated to
implementer phases) and watching the correctness dip.

**`terse` came out ≈cost-neutral per-worker here — it did NOT reproduce the earlier
+99%-on-haiku harm.** That earlier figure (see `2026-06-20-fair-test.md`) was the
per-worker leg measured on a 3-task over-build-prone *subset*; across all 5 tasks at
n=10 it washes out to roughly neutral (haiku −13%, sonnet −7%, opus +1%). The harm is
real but tail-concentrated and variance-sensitive: the `slugify/terse/haiku` cells
over-built to **LOC 32** (vs ~5–8 elsewhere) and ran ~50% longer, dragging that cell's
over-engineering to 0.5 — the same "terse makes the small model deliberate and over-build"
failure mode, just diluted in the mean.

> **This does NOT overturn the terse kill.** B1 was removed on the strength of the
> *whole-cycle* A/B (`2026-06-20-whole-cycle-ab.md`: +37.8% total tokens on sonnet, the
> deliberation cost compounding across turns + cache re-injection) and the deterministic
> capped-benefit proof — **neither of which this per-worker matrix re-measures**. This run
> is a post-hoc per-tier sweep, and on the per-worker axis it lands where the writeups
> said it would: directional, variance-heavy, tail-driven. If anything it underlines *why*
> the kill leaned on the whole-cycle measurement rather than the per-worker one.

**Safety held everywhere.** `safe_rate = 1.00` across all 12 (model × arm) cells on
safe-join. As in prior runs, bare is safe-by-default on this task, so the carve-out's
protective value is still not *demonstrated* — that needs a task where the bare baseline
drops the guard (ponytail's `safe-path` / `critic-email` shape).

## Caveats

- Per-worker only (isolated single `claude -p` edits) — not the whole-cycle pipeline.
- n=10 per (model, arm) rolls **heterogeneous** tasks together (over-build-prone:
  date-field, file-dropzone, form-validation; irreducible: slugify, safe-join), so a
  single rollup row mixes two regimes — read the per-arm deltas as directional.
- Over-engineering / correctness are LLM-judge scores (sonnet, temp 0); ±0.2–0.4 noise.
- Most tasks have a native solution (`<input type=date|file>`, HTML5 validation), so LOC
  deltas are small except where an arm trips into a custom build (the terse/haiku slugify
  tail, the opus minimal_diff 10→1 collapse).
