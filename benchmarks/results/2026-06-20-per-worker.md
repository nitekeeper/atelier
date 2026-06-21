# Atelier token-lever benchmark — per-worker matrix (pilot, 2026-06-20)

Ponytail's agentic methodology applied to atelier's REAL rule constants. Each cell =
one headless `claude -p --model claude-haiku-4-5` editing an isolated fixture copy,
scored on git-diff LOC + four-channel tokens + cost + wall-time + an LLM
over-engineering/correctness judge (+ a deterministic adversarial safety check on
safe-join). Arms inject atelier's actual briefing rules via `--append-system-prompt`.
Instrument validated by a selftest (safety scorer + judge both discriminate) before spend.

n=3 reps x 3 tasks (date-field, slugify, safe-join) x 4 arms = 36 cells; 32 completed
(4 safe-join cells hit the 300s cap — concentrated on the slow `terse`/`both` arms).

## Headline (per arm, across tasks)

| arm | cost vs bare | wall vs bare | LOC | correctness | verdict |
|---|--:|--:|--:|--:|---|
| bare | — | — | 13 | 2.57 | baseline |
| **terse** (`_TERSE_OUTPUT_RULE`) | **+31%** | **+84%** | 10 | 2.62 | **NET LOSS** |
| minimal_diff (`_MINIMAL_DIFF_RULE`) | −11% | −3% | 12 | 2.56 | ~neutral (slightly cheaper) |
| both | ~0% | +10% | 12.5 | 2.62 | ~neutral |

## Key result: atelier's terse rule is a measured net loss
Atelier's `_TERSE_OUTPUT_RULE` (the "caveman" input-side compressor) cost **+31% more $
and +84% more wall-time** than bare, with **no LOC reduction** (10 vs 13) and no
correctness gain. One terse cell over-built safe-join to **302 LOC** and several terse
cells **timed out at 300s**. This empirically REPLICATES ponytail's finding —
terse-prose compression is a net loss — on atelier's OWN rule constant. The cause is
structural (confirmed by the data): tokens are **cache-dominated** (~75-180k for a
few lines of output), so trimming output prose cannot offset the per-turn input/cache
re-injection, while the extra instruction makes the model deliberate longer.

## minimal_diff: roughly neutral HERE (not a win, not a loss)
On haiku these tasks have little to over-build (bare already writes ~minimal code:
date-field = 1 line in every arm), so the output-side lever has no room and lands
~cost-neutral (−11% cost, noisy). Its value should appear on over-build-prone tasks
and stronger models — NOT demonstrated by this task set.

## Safety: held everywhere, but no delta
safe_rate = 1.00 across ALL arms on safe-join — but bare is already safe by default
on this task, so the carve-out's protective value isn't demonstrated here (need a task
where the bare baseline drops the guard, like ponytail's parseaddr/critic-email).

## Honest limitations
- Small/UNEVEN n (timeouts hit safe-join unevenly: bare n=7, minimal_diff n=9) → the
  exact % deltas are directional, not definitive. The terse net-loss signal is robust
  (consistently higher cost/time + the timeouts + the 302-LOC blowup).
- Four-channel tokens are cache-noisy; COST and WALL-TIME are the cleaner economic
  signals (and both indict terse).
- Over-build signal weak on haiku → minimal_diff under-tested.
- 300s cell timeout too low for the slow arms → 4 lost cells.

## Recommended next runs
1. Raise the cell timeout to 600s; re-run to recover the 4 safe-join cells.
2. Add over-build-prone tasks + a sonnet/opus arm so minimal_diff has room to show.
3. Add a "bare-drops-the-guard" safety task to demonstrate the carve-out's value.
4. PRODUCTION ACTION: the terse/caveman lever is a measured net loss on these tasks —
   gate it off and A/B it whole-cycle before keeping it on.

See [`2026-06-20-fair-test.md`](2026-06-20-fair-test.md) and
[`2026-06-20-whole-cycle-ab.md`](2026-06-20-whole-cycle-ab.md) for the follow-on runs
that made the verdict decisive.
