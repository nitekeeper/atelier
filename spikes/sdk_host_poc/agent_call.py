"""agent_call — the single site where one agent call is fired.

``run_attempt`` is the only place in the host that runs an agent.  Every
invocation goes through:

1. Journal lookup — HIT returns cached envelope at $0 (no agent call).
2. Budget preflight — THROWS ``BudgetExceeded`` BEFORE any agent call.
3. Build options carrying ``output_format=ENVELOPE_SCHEMA`` (forced schema).
4. ``await asyncio.wait_for(_drain(query_fn(prompt, options=options)), WALL_CLOCK_S)``
5. ``validate_envelope(result.structured_output, ...)`` — fail-closed anti-spoof.
6. ``budget.charge(result.usage)``
7. ``journal.put(key, envelope, usage)``
8. Return envelope.

``WALL_CLOCK_S`` bounds a hung agent call; a ``TimeoutError`` propagates upward
to the scheduler which routes it as a failed attempt (same as any other
exception from here).

Transport
---------
The live agent path (M3) drives the installed ``claude`` CLI directly as a
subprocess — NOT the ``claude-agent-sdk`` package — using subscription auth (no
API key, no new dependency).  ``--json-schema`` gives native structured output
and ``--output-format json`` carries ``usage`` / ``total_cost_usd``.

Options object
--------------
M0 uses a lightweight :class:`SpikeOptions` dataclass mirroring the fields the
host threads into the ``claude`` invocation: ``model`` (→ ``--model``), ``cwd``
(clone-scoped writes), ``permission_mode`` (→ ``--permission-mode``),
``output_format`` (→ ``--json-schema``), ``max_turns``.  Crucially it carries
``output_format=ENVELOPE_SCHEMA`` so the "forced schema-validated return" half
of A3 is genuinely wired (the fake records the options it received and a test
asserts ``output_format is ENVELOPE_SCHEMA``).  In M3 ``SpikeOptions`` becomes
the assembled ``claude`` argv (flags above) — ``run_attempt`` logic is unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spikes.sdk_host_poc.budget import BudgetPool
from spikes.sdk_host_poc.envelope_schema import ENVELOPE_SCHEMA, validate_envelope
from spikes.sdk_host_poc.journal import ResultJournal

# Wall-clock deadline for a single agent call.  A hung call is cancelled and
# treated as a failed attempt by the scheduler.
WALL_CLOCK_S: float = 1800.0  # 30 min; tests override via the fake's sleep_s

# Conservative per-agent output-token estimate used when the caller does not
# supply an explicit estimate.  Calibrated for a mid-complexity sonnet-tier task.
DEFAULT_EST_PER_AGENT: int = 500

# Default max_turns passed in the spike options.  The real engine tunes this
# per-(phase, tier) in M3/M5; here it is a fixed placeholder.
DEFAULT_MAX_TURNS: int = 8

# Default permission mode (→ ``claude --permission-mode``).  The real engine
# hard-pins this and the cwd to the experiment clone in M3 (R1 — clone-escape
# refusal); M0 just records it.
DEFAULT_PERMISSION_MODE: str = "bypassPermissions"


@dataclass
class SpikeOptions:
    """Lightweight stand-in for the assembled ``claude`` invocation options.

    M0 only.  Mirrors the fields the host threads into the live ``claude``
    subprocess (``--model`` / ``cwd`` / ``--permission-mode`` / ``--json-schema``
    / ``max_turns``) so the fake can RECORD them and a test can assert
    ``output_format is ENVELOPE_SCHEMA``.  In M3 these become the real ``claude``
    argv.
    """

    model: str
    cwd: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    output_format: Any = None  # the ENVELOPE_SCHEMA JSON Schema object
    max_turns: int = DEFAULT_MAX_TURNS
    system_prompt: str | None = None


async def _drain(async_gen: Any) -> Any:
    """Consume an async generator, returning the last yielded item.

    The agent-call seam is an async generator that yields incremental messages
    and a terminal result message.  We consume all messages and return the
    terminal one (which carries ``.usage`` and ``.structured_output``).  In M3
    this drains the streamed ``claude --output-format json`` result.

    Raises ``RuntimeError`` if the generator yields nothing.
    """
    last = None
    async for msg in async_gen:
        last = msg
    if last is None:
        raise RuntimeError("query() async generator yielded no messages (unexpected empty stream)")
    return last


async def run_attempt(
    task: dict[str, Any],
    attempt: int,
    *,
    budget: BudgetPool,
    journal: ResultJournal,
    model: str,
    briefing: str,
    dag: dict[str, Any] | None = None,
    query_fn: Callable[..., Any],
    est_per_agent: int = DEFAULT_EST_PER_AGENT,
    upstream_envelope_hashes: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run one task attempt through the host dispatch pipeline.

    Parameters
    ----------
    task:
        Task dict with at least ``task_id``, ``assigned_persona``, ``phase``.
    attempt:
        Attempt number (1-indexed).
    budget:
        Shared ``BudgetPool``; asserted before dispatch and charged after.
    journal:
        ``ResultJournal``; checked for a cached result before dispatch.
    model:
        Model identifier (e.g. ``"claude-sonnet-4-5"``).
    briefing:
        Fully-rendered system prompt / briefing (must be clock/RNG-free).
    dag:
        Optional DAG structure (unused in M0; reserved for M4 pipeline safety).
    query_fn:
        Callable that accepts ``(prompt, options=...)`` and returns an async
        generator of result-message-shaped objects.  In tests this is
        ``FakeAgent().query_fn(task_id, attempt)``; in production (M3) it drives
        the ``claude`` CLI subprocess.
    est_per_agent:
        Estimated output tokens for the budget preflight check.
    upstream_envelope_hashes:
        Envelope hashes of transitive upstream dependencies for the journal key.
    cwd:
        Working directory hard-pinned into the options (the experiment clone in
        production).  M0 records it on :class:`SpikeOptions`; the real engine
        (M3) enforces clone-escape refusal here.

    Returns
    -------
    dict
        The validated envelope dict.

    Raises
    ------
    BudgetExceeded
        If the budget preflight fails (BEFORE any query).
    EnvelopeValidationError
        If the returned structured output fails anti-spoof validation.
    asyncio.TimeoutError
        If the query exceeds ``WALL_CLOCK_S``.
    """
    task_id = str(task.get("task_id", ""))

    # 1. Journal lookup — HIT → return at $0
    jkey = journal.key(
        task,
        attempt,
        model=model,
        briefing=briefing,
        upstream_envelope_hashes=upstream_envelope_hashes or [],
    )
    cached = journal.lookup(jkey)
    if cached is not None:
        return cached

    # 2. Budget preflight — THROWS BudgetExceeded BEFORE any agent call
    budget.assert_can_dispatch(est_per_agent)

    # 3. Build options carrying output_format=ENVELOPE_SCHEMA (forced schema).
    #    In M3 these become the assembled ``claude`` argv (--json-schema, --model,
    #    --permission-mode, cwd); here the lightweight SpikeOptions mirrors the
    #    threaded fields so the fake can record that output_format IS
    #    ENVELOPE_SCHEMA (the "forced schema-validated return" half of A3).
    prompt = briefing  # the briefing IS the system prompt in the spike
    options = SpikeOptions(
        model=model,
        cwd=cwd,
        output_format=ENVELOPE_SCHEMA,
        system_prompt=briefing,
    )

    # 4. Execute the agent call with a wall-clock deadline
    #    ``query_fn`` is either FakeAgent().query_fn(task_id, attempt) (returns a
    #    callable that produces an async generator) or, in M3, the ``claude``
    #    subprocess driver.
    result = await asyncio.wait_for(
        _drain(query_fn(prompt, options=options)),
        timeout=WALL_CLOCK_S,
    )

    # 5. Validate envelope — fail-closed anti-spoof
    envelope = validate_envelope(
        result.structured_output,
        dispatched_task_id=task_id,
        dispatched_attempt=attempt,
    )

    # 6. Charge budget (AFTER validation — a malformed envelope is a failed
    #    attempt and its tokens are NOT charged to the pool)
    budget.charge(result.usage)

    # 7. Persist to journal
    journal.put(jkey, envelope, usage=result.usage)

    # 8. Return validated envelope
    return envelope
