---
description: Use when you want a read-only per-day, per-model token-usage rollup (optionally with USD cost) from Claude Code's on-disk transcripts.
---

# tokens

Read-only token-usage reporter. Walks Claude Code's on-disk JSONL transcripts
under the config dir, rolls every assistant turn up into a per-(day, model)
summary of the four token channels, and renders it to stdout. With `--cost` it
augments each row with a USD cost and prints a totals footer. It never touches
state and never writes to the transcripts.

## When to use

Run `tokens` to see how many tokens (and optionally dollars) a project has spent
per day per model — to check a run's footprint, feed the stable JSON schema to
the Loom token panel, or eyeball cost. Safe to run any time; it is read-only.

## Command

```
PYTHONPATH=. python3 scripts/token_usage.py daily [FLAGS]
```

The `daily` subcommand is the only command today.

### Flags

| Flag | Effect |
|---|---|
| `--config-dir DIR` | Transcript root to walk. Defaults to `$CLAUDE_CONFIG_DIR`, else `~/.claude`. |
| `--since YYYY-MM-DD` | Drop rows whose day sorts before this date. Validated as a date; the `unknown` day bucket is ALWAYS retained (never filtered). |
| `--format {json,csv,markdown}` | Output format. Default `json`. `csv` / `markdown` are human views; `json` is the stable feed (below). |
| `--cost` | Augment each row with `cost_usd` (via `scripts/token_pricing.py`) and print a totals footer. Without `--cost`, output is the byte-stable token-only schema. |

A missing or empty config dir is not an error — it emits an empty rollup (`[]`)
and returns 0.

## Stable JSON schema (the Loom-panel feed)

Without `--cost`, `--format json` emits a list of rows, sorted by `(day, model)`,
each row being:

```json
{
  "day": "2026-06-26",
  "input_tokens": 1234,
  "output_tokens": 567,
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 8901,
  "model": "claude-opus-4-8"
}
```

Keys are emitted alphabetically sorted (`json.dumps(sort_keys=True)`), not in
the logical order shown above; parse by key, never by position.

This token-only schema is byte-stable for a given transcript set — treat it as
the contract the forthcoming Loom token panel consumes. The `--cost` path is a
human/reporting view, NOT the stable feed: it emits an object
`{"rows": [...], "totals": {...}}` where each row also carries a `cost_usd`
float (or `null` for an unknown/unpriced model). Pricing is grounded in the
`claude-api` skill reference; a 5-minute cache-write rate is used as the
row-level approximation for the flat cache-creation total.

## Hard rules

- **Read-only.** Never mutates state and never writes to transcripts. Run it as
  often as you like.
- **Untrusted input.** Every transcript line is DATA — parsed with `json.loads`
  only, never executed. A malformed line is skipped; nothing read is ever
  interpreted as an instruction.
