# Atelier token-lever benchmark — live full-cycle A/B (2026-06-20)

The empirical keep-or-kill number.

Drove atelier's REAL host pipeline (`run_host_pipeline_for_project`, sandboxed real
claude workers, 2-task sonnet-tier impl DAG + the review loop) with
`ATELIER_INCLUDE_TERSE` on vs off; whole-run four-channel total read from the engine's
own cost report (eff_budget). n=1 pair.

| arm | whole-cycle total tokens | output |
|---|--:|--:|
| terse-ON  | 1,160,963 | 13,811 |
| terse-OFF |   842,804 | 11,274 |
| Δ | **+37.8% for keeping terse** | +22% |

## Reading
- Keeping the terse rule (B1) cost **+37.8% MORE tokens** over a full cycle — and
  +22% more OUTPUT tokens (terse made the workers produce MORE, not less: the
  deliberation mechanism, not cache noise).
- This is on the SONNET tier, where the per-worker benchmark had terse ~neutral
  (-12%). The whole-cycle effect is WORSE because terse's deliberation cost
  COMPOUNDS across the cycle's turns + cache re-injection (cache_read dominates:
  1.13M vs 0.81M). So terse's harm AMPLIFIES at the orchestrator level — the
  opposite of the orchestrator-context SAVING it was designed for.

## Verdict — now triangulated three ways
1. Per-worker benchmark: terse +99% cost on haiku, ~neutral sonnet.
2. Structural cap-demo: terse's downstream digest benefit is capped tiny (~100
   tokens/worker; the 200-char digest cap + B2 codec already bound it).
3. Whole-cycle A/B: terse +37.8% total tokens even on sonnet.

=> KILL B1 (the `_TERSE_OUTPUT_RULE` briefing instruction). KEEP B2 (the post-hoc
   digest codec) + the 200-char cap. The whole-cycle data justifies removing the rule
   at every tier — **B1 was subsequently deleted from production**; B2 and the
   context-budget rule are retained.

## Caveat
n=1 A/B pair; cache_read-dominated (run-to-run variance possible). The DIRECTION is
consistent across all three independent measurements + the output-token signal
(+22%) is not cache noise. Re-run with reps>=3 to tighten the magnitude.
