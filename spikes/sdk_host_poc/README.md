# spikes/sdk_host_poc — Deterministic Host PoC

**Throwaway spike.** This directory contains a minimal standalone proof-of-concept that drives a deterministic Python host through all 5 ideas in the [Atelier v2 target architecture](../../docs/design/2026-06-13-atelier-v2-deterministic-engine.md) on a trivial 3-task DAG, using a stdlib-only fake agent — no API key, no network, no third-party dependencies on the test path.

**The 5 assertions in `test_poc.py` are the M1–M4 acceptance contract.**  Every later milestone must satisfy the same assertions against the live agent transport.

## Transport decision

The live agent path drives the installed **`claude` CLI directly as a subprocess** — **NOT** the `claude-agent-sdk` package. This means:

- **Subscription auth, no API key, no new dependency.** The host shells out to the `claude` binary already on the operator's PATH.
- **`--json-schema` → `structured_output`.** The `ENVELOPE_SCHEMA` is passed as `--json-schema` so the agent emits native structured output; the host validates it with atelier's real `validate_envelope`.
- **`--output-format json` → `usage` / `total_cost_usd`.** The JSON result carries the per-call token meter that feeds `BudgetPool`.
- **`--model` → `model_tier`.** Per-task tier selection from atelier's `model_tier.recommend`.
- **`--system-prompt` → lean per-agent briefing.** The composed briefing is the agent's system prompt.
- **`--permission-mode bypassPermissions` + `cwd` / `--add-dir` → clone-scoped writes.** Writes are hard-pinned to the experiment clone (R1 clone-escape refusal is enforced by the host in M3).

The PoC's logic is transport-agnostic: the fake already returns a CLI-result-shaped object with `.usage`, `.total_cost_usd`, and `.structured_output`, so swapping the fake for a real `claude` subprocess in M3 leaves `agent_call.run_attempt` unchanged.

## What runs in M0 vs M3

The `claude` binary is **not assumed present** in CI, so M0 runs entirely on a stdlib-only fake. Two seams are deliberately spike-shaped and become real in M3:

- **Options object.** `agent_call.run_attempt` builds a lightweight `SpikeOptions` dataclass carrying `output_format=ENVELOPE_SCHEMA` (+ `model`, `cwd`, `permission_mode`, `max_turns`). The fake *records* the options it receives, and `test_output_format_is_envelope_schema` asserts the fake actually got `output_format is ENVELOPE_SCHEMA` — so the "forced schema-validated return" half of A3 is genuinely wired, not merely declared. In M3 `SpikeOptions` becomes the assembled `claude` argv (`--json-schema`, `--model`, `--system-prompt`, `--permission-mode`).
- **Live agent path.** `fake_agent.claude_cli_available()` checks `shutil.which("claude")`. The `@pytest.mark.live` test (`test_live_cli_smoke`) exercises that seam; it is **deselected by default** and **skipped** when the binary is not on PATH. Driving `claude` end-to-end as a subprocess is an M3 task.

## How to run

```sh
# From the atelier repo root — the full spike suite (all green, no API key, no binary):
PYTHONPATH=. pytest spikes/sdk_host_poc/ -q
```

The whole suite runs in < 2 seconds with no API key. The `live` marker is registered in the spike-local `conftest.py`. To attempt the live CLI smoke (requires the `claude` binary on PATH — otherwise it is skipped):

```sh
PYTHONPATH=. pytest spikes/sdk_host_poc/ -q -m live
```
