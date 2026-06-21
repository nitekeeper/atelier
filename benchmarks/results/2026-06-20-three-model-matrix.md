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

**Where the correctness dip actually comes from — it is NOT broad, it is one task.**
Breaking correctness (and mean LOC) down per task, bare → minimal_diff, averaged across
all three models (n=6/cell):

| task | bare | minimal_diff | note |
|---|--:|--:|---|
| safe-join | 2.83 (loc 40) | **3.00 (loc 17)** | leaner **and** more correct — the lever working as designed |
| slugify | 3.00 (loc 9) | 3.00 (loc 6) | leaner, correctness held |
| file-dropzone | 3.00 (loc 2) | 3.00 (loc 1) | tie |
| date-field | 2.50 (loc 1) | 2.00 (loc 1) | −0.5 |
| **form-validation** | **2.83 (loc 32)** | **1.83 (loc 7)** | **−1.0 — the smoking gun** |

Almost the entire aggregate dip is **form-validation**, a *compound* task with three
stated requirements (valid email, password ≥ 8 chars, block submit on invalid).
`minimal_diff` cut it 32 → 7 lines and the judge marks it down a full point — i.e. it
**dropped a requirement**: "build the minimum" overshot into "build less than asked." On
single-requirement tasks (slugify) and the safety task it is neutral-to-**better**
(`safe-join`: 40 → 17 lines AND correctness 2.83 → 3.00).

So the completeness risk is **localized to multi-acceptance-criteria work**, where the
"stop at the first rung that works" reflex can stop before covering every criterion — and
it is the lever violating its OWN carve-out ("do NOT minimize away … anything the task
EXPLICITLY requested"). `minimal_diff` stays paired with a review loop, and **correctness,
not LOC, is the metric to watch.**

> **Follow-up tried — a sharpened prompt clause did NOT fix it.** A `COVER EVERY
> REQUIREMENT` clause was added to `_MINIMAL_DIFF_RULE` and the matrix re-run (v2, n=2).
> form-validation `minimal_diff` correctness moved 1.83 → 2.00 — but the entire +0.17 is a
> single sonnet cell, the worst cell (haiku `[1, 2]`) did not move, and an **unchanged**
> control arm (`terse`) drifted the *same* +0.17, so it is indistinguishable from
> resampling noise at n=2. Conclusion: a blanket prompt instruction can't reliably stop a
> model dropping a requirement under brevity pressure. See the per-part policy below — the
> right fix is a *deterministic gate*, not a prompt nudge.

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

## Conclusion — the decision unit is mechanism × context, not whole levers

The honest read across all three runs is **not** "lever X good, lever Y bad." Every lever
won in some parts and lost in others; the value is in routing each *mechanism* to the
*context* where it actually helps, and gating it off where it doesn't. A whole-lever
keep/kill verdict is the wrong granularity.

**We already proved this once — and it worked.** "Caveman" was never one thing: it was
**B1**, the terse *briefing instruction* (a net loss → removed), and **B2**, the *post-hoc
codec* that strips filler after the text is written (free, harmless → kept). Same name,
two mechanisms, opposite verdicts. Splitting them was the win. The same discipline applies
one level deeper to `minimal_diff`.

### Per-part lever policy (what wins where)

| mechanism | context (part) | verdict | how to apply |
|---|---|---|---|
| **B2 codec** (post-hoc filler strip) | any orchestrator-facing prose | **win** (free, no agent cost) | keep, always |
| **B1 terse instruction** (tell agent to be brief) | whole cycle, every tier | **loss** (deliberation compounds) | removed; if revisited, gate **off for haiku** specifically, where the harm concentrates — never globally |
| **minimal_diff ladder** | over-build-prone, single-output (dropzone, date-field) | **strong win** (LOC/cost ↓, correctness held) | keep ON |
| **minimal_diff ladder** | irreducible single-requirement (slugify, safe-join) | **win** (leaner, correctness held-or-up) | keep ON |
| **minimal_diff ladder** | **compound / multi-requirement (form-validation)** | **loss** (drops a requirement; worst on haiku) | **gate, don't preach** — see below |

### The open follow-up: gate minimal_diff on compound tasks deterministically

The form-validation failure is the only place `minimal_diff` loses, and a prompt clause
didn't fix it (above). The robust fix is a **deterministic gate on a signal we already
have**: `compose_briefing` receives `acceptance_criteria`, so "compound task" is literally
`len(acceptance_criteria) > 1`. On that signal, either (a) soften / suppress the
minimal-diff ladder, or (b) keep it but attach a **per-criterion completeness check in the
review loop** — catch the dropped requirement *mechanically*, after the fact, instead of
hoping the implementer prompt prevents it. That is the lever applied *by part*, on a signal
the engine already passes — not a hope.

(The `COVER EVERY REQUIREMENT` clause added in the meantime is the right *idea* at the
wrong *granularity*: a blanket implementer-prompt where a `len(acceptance_criteria)`-gated
mechanism belongs. It is harmless and implementer-phase-only, but unproven — treat the
deterministic gate above as the real work.)
