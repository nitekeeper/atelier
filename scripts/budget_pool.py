"""BudgetPool — drift-buffered token-ceiling for the deterministic host.

Standalone, stdlib-only.  Lifted from the M0 PoC (spikes/sdk_host_poc/budget.py)
and adapted for production use.

Design notes
------------
* Gates on ``output_tokens`` (deterministic across pricing changes) rather than
  ``total_cost_usd`` (which drifts when Anthropic changes pricing or when the
  ``claude`` CLI does not recognise a model).
* ``effective_ceiling = total_tokens * headroom`` (default 0.70) — the 30 % buffer
  absorbs client-side estimate drift.  ``assert_can_dispatch`` fires PRE-call.
* Parent bubbling: a child's ``charge()`` increments the parent's spent counter so
  nested fix-loops share the global pool.
* ``static_fleet_width(budget, per_agent_tokens, max_workers)`` returns how many
  agents can be launched given the remaining budget, capped at ``max_workers``
  (never exceeds the caller-supplied ceiling).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class BudgetExceeded(RuntimeError):
    """Raised by :meth:`BudgetPool.assert_can_dispatch` when the effective ceiling
    would be exceeded by the estimated per-agent spend.

    This is terminal — the caller must abandon and escalate; it must NOT re-queue.
    """

    def __init__(self, spent: int, est: int, ceiling: int) -> None:
        self.spent = spent
        self.est = est
        self.ceiling = ceiling
        super().__init__(
            f"BudgetExceeded: spent={spent} + est={est} > ceiling={ceiling}; "
            "route to abandon+escalate, do not re-queue."
        )


class BudgetPool:
    """Shared output-token budget for a set of agent dispatches.

    Parameters
    ----------
    total_tokens:
        Hard limit in output tokens (before headroom scaling).
    headroom:
        Fraction of ``total_tokens`` that forms the *effective* ceiling
        (default 0.70 — 30 % drift buffer).
    parent:
        Optional parent pool.  When present, every :meth:`charge` also
        increments the parent's spent counter so nested sub-procedures share
        the global pool.
    """

    def __init__(
        self,
        total_tokens: int,
        headroom: float = 0.70,
        parent: BudgetPool | None = None,
    ) -> None:
        if not (0.0 < headroom <= 1.0):
            raise ValueError(f"headroom must be in (0, 1]; got {headroom!r}")
        if total_tokens <= 0:
            raise ValueError(f"total_tokens must be positive; got {total_tokens!r}")
        self._total = total_tokens
        self._headroom = headroom
        self._spent: int = 0
        self._parent = parent
        # Side counters for post-run Usage&Cost reconciliation.  These are
        # accumulated and bubbled exactly like ``output_tokens`` but NEVER gate
        # ``assert_can_dispatch`` — the ceiling stays output-token-only by design.
        self._input_tokens: int = 0
        self._cache_creation_input_tokens: int = 0
        self._cache_read_input_tokens: int = 0

    # ── read-only properties ───────────────────────────────────────────────

    @property
    def effective_ceiling(self) -> int:
        """``total_tokens * headroom``, *floored*.

        This is ``int(total_tokens * headroom)`` — it FLOORS the IEEE-754
        product (e.g. ``3 * 0.7 == 2.0999… → 2``), not ``round``.  The floor is
        conservative by design: the realised ceiling is never higher than the
        true ``total * headroom``, so a tiny float drift can only *tighten* the
        budget, never loosen it.
        """
        return int(self._total * self._headroom)

    def spent(self) -> int:
        """Accumulated output tokens charged so far (self only, not parent)."""
        return self._spent

    def remaining(self) -> int:
        """Tokens still available: ``effective_ceiling - spent``."""
        return max(0, self.effective_ceiling - self._spent)

    def usage_breakdown(self) -> dict[str, int]:
        """Accumulated per-channel token counters for cost reconciliation.

        Returns a dict with ``output_tokens`` (the gated counter, == ``spent()``)
        plus the non-gating side counters: ``input_tokens``,
        ``cache_creation_input_tokens``, ``cache_read_input_tokens``.  These back
        the design's post-run Usage&Cost reconciliation; only ``output_tokens``
        ever drives :meth:`assert_can_dispatch`.
        """
        return {
            "output_tokens": self._spent,
            "input_tokens": self._input_tokens,
            "cache_creation_input_tokens": self._cache_creation_input_tokens,
            "cache_read_input_tokens": self._cache_read_input_tokens,
        }

    # ── mutating operations ────────────────────────────────────────────────

    def charge(self, usage: Mapping[str, Any]) -> None:
        """Accumulate the ``usage`` token channels into the running totals.

        ``output_tokens`` drives the gated ``spent()`` counter (the only channel
        ``assert_can_dispatch`` checks).  ``input_tokens``,
        ``cache_creation_input_tokens`` and ``cache_read_input_tokens`` are
        accumulated into non-gating side counters for cost reconciliation.

        All channels bubble to the parent (if one is set) so nested fix-loop
        children share the global pool and its reconciliation totals.

        Parameters
        ----------
        usage:
            Mapping mirroring the ``usage`` block in the ``claude
            --output-format json`` result.  Missing channels are treated as 0.
        """
        out = int(usage.get("output_tokens", 0))
        inp = int(usage.get("input_tokens", 0))
        cache_create = int(usage.get("cache_creation_input_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))

        self._spent += out
        self._input_tokens += inp
        self._cache_creation_input_tokens += cache_create
        self._cache_read_input_tokens += cache_read

        if self._parent is not None:
            # Bubble every channel one level up, preserving the full breakdown
            # so the parent's reconciliation totals match the sum of its children.
            self._parent.charge(
                {
                    "output_tokens": out,
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cache_create,
                    "cache_read_input_tokens": cache_read,
                }
            )

    def assert_can_dispatch(self, est_per_agent: int) -> None:
        """Assert the estimated per-agent spend fits inside the effective ceiling.

        Raises :class:`BudgetExceeded` if ``spent + est_per_agent > effective_ceiling``.
        This check fires BEFORE the CLI subprocess is launched — the caller must
        abort the dispatch and route to abandon+escalate on exception.

        Parameters
        ----------
        est_per_agent:
            Estimated output tokens the next agent call will consume.
        """
        if self._spent + est_per_agent > self.effective_ceiling:
            raise BudgetExceeded(
                spent=self._spent,
                est=est_per_agent,
                ceiling=self.effective_ceiling,
            )

    # ── static helpers ─────────────────────────────────────────────────────

    @staticmethod
    def static_fleet_width(
        budget: BudgetPool,
        per_agent_tokens: int,
        max_workers: int,
    ) -> int:
        """How many agents can be launched given the remaining budget.

        Returns ``min(max_workers, remaining() // per_agent_tokens)``.
        This value *only ever narrows* the caller-supplied ``max_workers`` ceiling —
        it never widens it.

        Parameters
        ----------
        budget:
            The :class:`BudgetPool` to query.
        per_agent_tokens:
            Estimated output tokens per agent call.
        max_workers:
            Upper bound set by the caller (e.g. ``MAX_PARALLEL_WORKERS``).

        Returns
        -------
        int
            Number of agents that may be launched (0 when budget is exhausted).
        """
        if per_agent_tokens <= 0:
            raise ValueError(f"per_agent_tokens must be positive; got {per_agent_tokens!r}")
        if max_workers <= 0:
            raise ValueError(f"max_workers must be positive; got {max_workers!r}")
        budget_width = budget.remaining() // per_agent_tokens
        return min(max_workers, budget_width)
