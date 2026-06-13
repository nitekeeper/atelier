"""BudgetPool — drift-buffered token-ceiling for the deterministic host.

Standalone, stdlib-only.  Lifted verbatim into M1.

Design notes
------------
* Gates on ``output_tokens`` (deterministic across pricing changes) rather than
  ``total_cost_usd`` (which drifts when Anthropic changes pricing or when the
  ``claude`` CLI does not recognise a model).
* ``effective_ceiling = total_tokens * headroom`` (default 0.70) — the 30 % buffer
  absorbs client-side estimate drift.  ``assert_can_dispatch`` fires PRE-call.
* Parent bubbling: a child's ``charge()`` increments the parent's spent counter so
  nested fix-loops share the global pool.
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

    # ── read-only properties ───────────────────────────────────────────────

    @property
    def effective_ceiling(self) -> int:
        """``total_tokens * headroom``, rounded down."""
        return int(self._total * self._headroom)

    def spent(self) -> int:
        """Accumulated output tokens charged so far (self only, not parent)."""
        return self._spent

    def remaining(self) -> int:
        """Tokens still available: ``effective_ceiling - spent``."""
        return max(0, self.effective_ceiling - self._spent)

    # ── mutating operations ────────────────────────────────────────────────

    def charge(self, usage: Mapping[str, Any]) -> None:
        """Add ``usage['output_tokens']`` to the running total.

        Bubbles to parent if one is set, so nested fix-loop children share the
        global pool.

        Parameters
        ----------
        usage:
            Mapping containing at least ``output_tokens`` (int).  Mirrors the
            ``usage`` block in the ``claude --output-format json`` result.
        """
        tokens = int(usage.get("output_tokens", 0))
        self._spent += tokens
        if self._parent is not None:
            self._parent.charge({"output_tokens": tokens})

    def assert_can_dispatch(self, est_per_agent: int) -> None:
        """Assert the estimated per-agent spend fits inside the effective ceiling.

        Raises :class:`BudgetExceeded` if ``spent + est_per_agent > effective_ceiling``.
        This check fires BEFORE ``query()`` — the caller must abort the dispatch
        and route to abandon+escalate on exception.

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
