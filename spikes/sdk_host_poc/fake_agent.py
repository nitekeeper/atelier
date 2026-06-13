"""Fake / record-replay agent for the deterministic host PoC.

The live agent path drives the installed ``claude`` CLI directly as a
subprocess (subscription auth, no API key, no third-party dependency) — see
``README.md`` for the exact flags.  This module provides a stdlib-only fake of
that one agent call so the whole PoC runs in CI with no ``claude`` binary:

* **Stdlib-only** — no third-party dependencies on the fake path.
* **Configurable per-task** — return a given envelope, set ``output_tokens``,
  sleep a configurable duration (to test pipeline timing), or return a
  malformed envelope (wrong ``task_id``).
* **Record/replay aware** — records every call (count + args) so tests can
  assert exact call counts.
* **Seam-friendly** — ``FakeAgent`` exposes a ``query_fn`` method whose
  async-generator interface mirrors the host's single agent-call seam.  The
  live ``claude``-binary check is lazy/guarded so CI runs with no binary.

The fake returns a CLI-result-shaped object: ``.usage`` (from
``--output-format json``), ``.total_cost_usd`` (ditto), and ``.structured_output``
(from ``--json-schema``).  These are exactly the fields the host reads, so the
PoC logic is transport-agnostic — swapping the fake for a real ``claude``
subprocess in M3 leaves ``agent_call.run_attempt`` unchanged.

Default envelope determinism
-----------------------------
When no explicit envelope is configured, the fake builds a default envelope
that includes a sha256 of the prompt text.  This ensures a changed briefing
(prompt) produces a *different* envelope hash, which in turn triggers the
upstream-digest cascade invalidation in ``ResultJournal`` (A5).

Live-binary guard
-----------------
:func:`claude_cli_available` checks ``shutil.which("claude")`` lazily so
``import fake_agent`` never fails in CI and the live smoke test deselects/skips
cleanly when the binary is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from typing import Any


def _prompt_tag(prompt: str) -> str:
    """Short hex tag derived from the prompt — makes default envelopes
    prompt-dependent so a changed briefing yields a different envelope hash
    (which drives upstream-digest cascade invalidation in A5)."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


# ── ResultMessage-shaped return object ────────────────────────────────────────


class FakeResultMessage:
    """Minimal stand-in for the parsed ``claude --output-format json`` result.

    Attributes match what ``agent_call.run_attempt`` reads:
    * ``.usage``            — dict with ``input_tokens``, ``output_tokens``, etc.
    * ``.total_cost_usd``  — float (client-side estimate; not used in budget math).
    * ``.structured_output`` — the validated envelope dict (from ``--json-schema``).
    """

    def __init__(
        self,
        structured_output: dict[str, Any],
        output_tokens: int = 100,
        input_tokens: int = 500,
        total_cost_usd: float = 0.001,
    ) -> None:
        self.structured_output = structured_output
        self.usage: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self.total_cost_usd = total_cost_usd


# ── FakeAgent ─────────────────────────────────────────────────────────────────


class FakeAgent:
    """Configurable fake of the host's single agent-call seam.

    Usage::

        fake = FakeAgent()
        fake.configure("t1", envelope={...}, output_tokens=200, sleep_s=0.0)
        fake.configure("t2", envelope={...}, output_tokens=300, sleep_s=0.05)

        envelopes = await run_dag(tasks, budget=bp, journal=rj, fake_agent=fake)
        assert fake.call_count == 2

    Parameters
    ----------
    default_output_tokens:
        Fallback ``output_tokens`` for tasks with no explicit config.
    default_sleep_s:
        Fallback sleep duration (seconds) for tasks with no explicit config.
    """

    def __init__(
        self,
        default_output_tokens: int = 100,
        default_sleep_s: float = 0.0,
    ) -> None:
        self._default_output_tokens = default_output_tokens
        self._default_sleep_s = default_sleep_s
        # Per-task configs: task_id → config dict
        self._configs: dict[str, dict[str, Any]] = {}
        # Call log: list of (task_id, attempt, prompt, options)
        self._calls: list[dict[str, Any]] = []

    # ── configuration ─────────────────────────────────────────────────────

    def configure(
        self,
        task_id: str,
        *,
        envelope: dict[str, Any] | None = None,
        output_tokens: int | None = None,
        sleep_s: float | None = None,
        malformed: bool = False,
        wrong_task_id: str | None = None,
    ) -> None:
        """Set per-task fake behaviour.

        Parameters
        ----------
        task_id:
            The ``task_id`` this config applies to.
        envelope:
            Envelope dict to return.  If ``None`` a default well-formed
            envelope is built at call time.
        output_tokens:
            ``output_tokens`` to report in ``usage``.
        sleep_s:
            Seconds to sleep before returning (to simulate wall-clock latency).
        malformed:
            If True, return an envelope with ``type`` set to ``"wrong_type"``
            (fails ``validate_envelope`` check 1).
        wrong_task_id:
            If set, return an envelope with ``task_id`` set to this string
            instead of the dispatched task_id (triggers anti-spoof failure).
        """
        self._configs[task_id] = {
            "envelope": envelope,
            "output_tokens": output_tokens,
            "sleep_s": sleep_s,
            "malformed": malformed,
            "wrong_task_id": wrong_task_id,
        }

    # ── query interface ───────────────────────────────────────────────────

    async def _generate(
        self,
        task_id: str,
        attempt: int,
        prompt: str,
        options: Any,
    ) -> FakeResultMessage:
        """Internal: async generator body that yields a FakeResultMessage.

        Separated so ``query_fn`` can wrap it as an async generator matching the
        host's agent-call seam (``async for msg in query_fn(...)``).
        """
        cfg = self._configs.get(task_id, {})
        sleep_s = cfg.get("sleep_s") if cfg.get("sleep_s") is not None else self._default_sleep_s
        output_tokens = (
            cfg.get("output_tokens")
            if cfg.get("output_tokens") is not None
            else self._default_output_tokens
        )

        if sleep_s and sleep_s > 0:
            await asyncio.sleep(sleep_s)

        # Build the envelope
        if cfg.get("malformed"):
            structured_output = {
                "type": "wrong_type",  # will fail validate_envelope check 1
                "task_id": task_id,
                "attempt": attempt,
                "status": "done",
                "artifacts": [{"path": "a.txt", "sha": "abc"}],
            }
        elif cfg.get("wrong_task_id"):
            wrong_id = cfg["wrong_task_id"]
            structured_output = {
                "type": "task_result",
                "task_id": wrong_id,  # anti-spoof: wrong task_id
                "attempt": attempt,
                "status": "done",
                "artifacts": [{"path": "a.txt", "sha": "abc"}],
            }
        elif cfg.get("envelope") is not None:
            structured_output = dict(cfg["envelope"])
        else:
            # Default well-formed envelope for this task.
            # NOTE: the prompt tag in 'notes_md' is deliberately included so
            # that a changed briefing/prompt produces a different envelope hash,
            # which drives upstream-digest cascade invalidation in the journal
            # (A5).  The hash is deterministic (sha256 of prompt), never RNG.
            prompt_tag = _prompt_tag(prompt)
            structured_output = {
                "type": "task_result",
                "task_id": task_id,
                "attempt": attempt,
                "status": "done",
                "artifacts": [{"path": f"{task_id}.txt", "sha": "deadbeef"}],
                "notes_md": f"Task {task_id} completed. prompt_tag={prompt_tag}",
            }

        return FakeResultMessage(
            structured_output=structured_output,
            output_tokens=output_tokens,
        )

    def query_fn(self, task_id: str, attempt: int):  # type: ignore[return]
        """Return an async-generator factory matching the host's agent-call seam.

        The returned callable, when called as ``query_fn(task_id, attempt)(prompt,
        options=...)``, yields exactly one ``FakeResultMessage`` (the terminal
        result).

        ``agent_call.run_attempt`` calls::

            async for msg in query_fn(prompt, options=options):
                last = msg
            result = last

        which works because we yield exactly one message.
        """

        async def _gen(prompt: str, options: Any = None):
            # Record the call BEFORE sleeping/returning so assert_call_count works
            # even if the caller awaits
            self._calls.append(
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "prompt": prompt,
                    "options": options,
                }
            )
            msg = await self._generate(task_id, attempt, prompt, options)
            yield msg

        return _gen

    # ── introspection ─────────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        """Total number of agent calls recorded."""
        return len(self._calls)

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Read-only view of recorded calls."""
        return list(self._calls)

    def reset(self) -> None:
        """Clear the call log (does not reset per-task configs)."""
        self._calls.clear()


# ── Live-binary guard ─────────────────────────────────────────────────────────


def claude_cli_available() -> bool:
    """Return whether the ``claude`` CLI binary is on PATH.

    The live agent path (M3) drives ``claude`` as a subprocess; this lazy check
    lets the live smoke test skip cleanly in CI where the binary is absent.
    """
    return shutil.which("claude") is not None
