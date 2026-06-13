"""CliDispatchTools — the real ``claude -p`` adapter (M3, behind a flag).

This is the production leaf of the deterministic-host engine: it turns ONE task
attempt into ONE **metered, schema-validated, journaled** ``claude -p`` call.
It is the real version of the M0 PoC ``run_attempt`` seam and the
``CliDispatchTools`` analog of the bridge's ``QueueBridgeDispatchTools``.

It is reachable ONLY when the operator opts into ``ATELIER_TRANSPORT=cli`` — the
bridge (``ATELIER_TRANSPORT=bridge``, the default) is untouched. M3 builds the
per-task adapter + the engine seams; the pipeline / scheduler wiring is M4.

Pipeline of one attempt (:func:`run_attempt`):

  1. Journal lookup — a HIT returns the cached envelope at $0 (no subprocess).
  2. ``budget.assert_can_dispatch(est)`` — raises ``BudgetExceeded`` BEFORE any
     subprocess (terminal: route to abandon+escalate, never re-queue).
  3. **R1 CLONE-ESCAPE PATH-GUARD** (defense-in-depth) — resolve the effective cwd
     + ``--add-dir`` to real paths and assert they are INSIDE ``clone_dir``; refuse
     (raise :class:`CloneEscapeError`) BEFORE spawning if anything escapes.
  4. Build argv as a LIST (never a shell string) and run it with ``cwd=clone``,
     bounded by ``asyncio.wait_for(WALL_CLOCK_S)``.
  5. Parse stdout JSON. ``is_error`` / bad ``subtype`` / missing
     ``structured_output`` / non-zero exit / timeout → a FAILED attempt sentinel
     (:data:`FAILED_ATTEMPT`) the engine routes through ``_handle_failed_attempt``
     — and NOT journaled.
  6. ``validate_envelope(structured_output, dispatched_task_id, dispatched_attempt)``
     — fail-closed, anti-spoof against the host's OWN dispatch identity.
  7. ``budget.charge(usage)``; ``journal.put(key, env, usage)``; return env.

Security posture (HARDENED — see the M3 review + the live confinement probe
captured in this module's tests):

* **The CLI permission layer does NOT confine writes (escalation finding).** A
  live probe proved BOTH ``--permission-mode acceptEdits`` AND
  ``bypassPermissions`` (with ``cwd``/``--add-dir`` pinned to the clone) STILL let
  a real agent write OUTSIDE the clone, with ``permission_denials: []``. The
  permission layer is NOT a containment boundary. The R1 path-guard is therefore
  **defense-in-depth only** (it stops the host from *configuring* an escape, not
  the agent from *performing* one).
* **A real, write-capable agent is REFUSED without a sandbox (mandatory-sandbox
  gate).** :func:`run_attempt` raises :class:`UnsandboxedRealRunError` when a REAL
  runner (``real_cli_runner``) would spawn with an identity ``sandbox_wrap`` —
  unless the operator attests the whole host is already OS-confined via
  ``ATELIER_CLI_ALLOW_UNSANDBOXED=1``. ``FakeCliRunner`` (no real process) is
  exempt. This is the escalation the review demanded once ``acceptEdits`` was
  shown not to confine.
* **Default permission mode is ``acceptEdits``, never ``bypassPermissions``.**
  ``bypassPermissions`` is additionally REFUSED (:class:`UnsandboxedBypassError`)
  unless a real sandbox is wired.
* **Default deny-list: ``Bash WebFetch WebSearch``.** Bash is the widest escape
  primitive; WebFetch/WebSearch are the egress/exfil primitives. Configurable,
  default-deny these three. (Defense-in-depth — not a containment boundary on its
  own, per the probe above.)
* **Minimal subprocess env.** ``real_cli_runner`` passes an explicit allowlisted
  ``env=`` (PATH/HOME/USER/LANG/LC_*/TERM + credential-path vars) — the full
  parent env (GH_TOKEN, cloud creds, ANTHROPIC_API_KEY, …) is NOT inherited into
  an autonomous agent. Subscription auth rides ``$HOME/.claude/.credentials.json``.
* **Sandbox seam (``sandbox_wrap``).** Injectable ``argv -> argv``. The default
  identity wrap fires a ONE-TIME loud "UNSANDBOXED" warning AND (with a real
  runner) trips the mandatory-sandbox gate above. :func:`bwrap_sandbox_wrap`
  enables Claude's native bubblewrap sandbox with ``filesystem.allowWrite=[clone]``
  + ``failIfUnavailable=true`` — fail-closed: the CLI REFUSES TO START when
  bubblewrap is absent (verified live), so an unconfined agent never runs.
  **M7 (defaulting CLI transport ON) is BLOCKED until a real OS-level sandbox is
  wired into this seam** (bwrap installed, or an external container/namespace
  wrapper).
* The subprocess is launched with ``create_subprocess_exec`` + an argv LIST (no
  ``shell``) — discrete argv items, never a shell string (no metacharacter
  interpretation). See ``# nosec B603``.
* **Reap on cancel.** ``real_cli_runner`` kills + reaps the child on
  cancellation/timeout so a wall-clock trip never leaks a zombie ``claude``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.budget_pool import BudgetPool
from scripts.dispatch import TRANSPORT_CLI
from scripts.envelope_schema import ENVELOPE_SCHEMA

# WALL_CLOCK_S (engine per-attempt deadline) is re-homed onto the subprocess: the
# CLI call is bounded by `asyncio.wait_for(WALL_CLOCK_S)`. On a timeout the
# coroutine is cancelled AND `real_cli_runner` kills+reaps the child `claude`
# (see its try/finally) — so a hung invocation is genuinely terminated, not left
# as a zombie. Imported from the engine so the adapter's wall clock stays
# single-sourced. INVARIANT (asserted in `run_attempt`): the adapter's
# `wall_clock_s` MUST be <= the engine's WALL_CLOCK_S, else a future *lower*
# engine deadline would be silently gated behind this subprocess wait_for,
# contradicting the termination proof's per-attempt bound.
from scripts.pm_dispatch import WALL_CLOCK_S
from scripts.pm_dispatch_envelope import validate_envelope
from scripts.result_journal import ResultJournal

# ── Sentinel for a failed attempt ──────────────────────────────────────────


class _FailedAttempt:
    """Singleton sentinel returned by :func:`run_attempt` for a failed attempt.

    A failed attempt (CLI ``is_error`` / bad ``subtype`` / missing
    ``structured_output`` / non-zero exit / wall-clock timeout) is NOT a valid
    envelope and is NOT journaled — the engine routes it through
    ``_handle_failed_attempt`` exactly like a silently-dead worker (the
    ``poll_fn`` returns ``None`` for it; see :func:`build_cli_poll_fn`).
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<FAILED_ATTEMPT reason={self.reason!r}>"


#: The canonical "this attempt failed" marker. Identity-comparable; a fresh
#: instance with a populated ``reason`` is also returned for diagnostics, so
#: callers test with ``isinstance(x, _FailedAttempt)`` (or ``is_failed_attempt``)
#: rather than ``is FAILED_ATTEMPT``.
FAILED_ATTEMPT = _FailedAttempt()


def is_failed_attempt(value: Any) -> bool:
    """True iff *value* is the failed-attempt sentinel (any instance)."""
    return isinstance(value, _FailedAttempt)


# ── Exceptions ─────────────────────────────────────────────────────────────


class CloneEscapeError(RuntimeError):
    """Raised by the R1 clone-escape guard when a resolved cwd / ``--add-dir``
    falls OUTSIDE the experiment clone.

    HIGHEST-SEVERITY safety invariant: ``bypassPermissions`` removes the human
    gate, and a prior real incident had a wave-1 teammate write into live
    ``~/apps/atelier``. This is host-enforced — the guard refuses BEFORE any
    subprocess is spawned (the runner is NEVER called).
    """

    def __init__(self, offending: str, clone_dir: str) -> None:
        self.offending = offending
        self.clone_dir = clone_dir
        super().__init__(
            f"clone-escape REFUSED: {offending!r} resolves outside the experiment "
            f"clone {clone_dir!r}; refusing to spawn (R1, defense-in-depth). The "
            "host hard-pins cwd to the clone; OS-level confinement is the sandbox "
            "seam's job (the permission layer does NOT confine writes — see module "
            "docstring)."
        )


class UnsandboxedBypassError(RuntimeError):
    """Raised when ``--permission-mode bypassPermissions`` is requested WITHOUT a
    sandbox wired into the ``sandbox_wrap`` seam.

    ``bypassPermissions`` disables every permission gate — a live probe proved an
    agent then writes anywhere on the host. It is therefore only safe inside a
    real OS-level sandbox (bubblewrap/container/restricted-user). With no sandbox,
    the host REFUSES it and falls back to (or demands) ``acceptEdits``.
    """

    def __init__(self) -> None:
        super().__init__(
            "permission_mode='bypassPermissions' refused: it disables all "
            "permission gates and a live probe proved it lets the agent write "
            "outside the clone. It is permitted ONLY when a sandbox is wired via "
            "the sandbox_wrap seam (e.g. bwrap_sandbox_wrap). Use the default "
            "'acceptEdits' instead, or wire a real OS-level sandbox."
        )


class UnsandboxedRealRunError(RuntimeError):
    """Raised when a REAL ``claude`` subprocess would be spawned with NO sandbox.

    **The escalation finding (proven live):** neither ``acceptEdits`` nor
    ``bypassPermissions`` confines writes to the clone — a real agent under either
    mode wrote OUTSIDE the clone with ``permission_denials: []``. The CLI
    permission layer is therefore NOT a containment boundary. So the host REFUSES
    to spawn a real, write-capable agent unless a real OS-level sandbox is wired
    via ``sandbox_wrap`` (e.g. :func:`bwrap_sandbox_wrap`, which fails closed when
    bubblewrap is absent).

    The ONLY way past this without wiring a sandbox is the explicit operator
    opt-out :data:`UNSANDBOXED_OPT_OUT_ENV` (``ATELIER_CLI_ALLOW_UNSANDBOXED=1``),
    intended for callers whose ENTIRE host is already OS-confined (a throwaway
    container/VM). It is OFF by default — fail-closed.

    The :class:`FakeCliRunner` (and any non-real runner) is exempt: it spawns no
    process, so there is nothing to contain.
    """

    def __init__(self) -> None:
        super().__init__(
            "refusing to spawn a REAL claude agent with no OS sandbox: the "
            "permission layer (acceptEdits/bypassPermissions) does NOT confine "
            "writes (proven live — an agent wrote outside the clone with empty "
            "permission_denials). Wire a real sandbox via sandbox_wrap "
            "(e.g. bwrap_sandbox_wrap(clone_dir)); or, ONLY if the whole host is "
            f"already OS-confined, set {UNSANDBOXED_OPT_OUT_ENV}=1 to opt out. "
            "M7 (defaulting CLI transport ON) stays BLOCKED until the sandbox "
            "seam carries a real OS sandbox."
        )


#: Operator opt-out for the mandatory-sandbox gate. Set to ``"1"`` ONLY when the
#: entire host is already OS-confined (throwaway container/VM) so the in-process
#: sandbox is redundant. OFF by default — the gate is fail-closed.
UNSANDBOXED_OPT_OUT_ENV = "ATELIER_CLI_ALLOW_UNSANDBOXED"


# ── Security posture constants (M3 review hardening) ───────────────────────

#: Default permission mode. ``acceptEdits`` (NOT ``bypassPermissions``) — in
#: headless ``-p`` it auto-accepts edits without a human prompt but still routes
#: through the permission layer (so a wired sandbox / future deny-rule can bite).
#: ``bypassPermissions`` is refused unless a sandbox is wired (see above).
DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Default tool deny-list. Bash is the widest filesystem/process escape; WebFetch
#: / WebSearch are the egress/exfil primitives. Default-deny all three; callers
#: may override ``disallowed_tools`` but these are the floor.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = ("Bash", "WebFetch", "WebSearch")

#: Subprocess env allowlist. ONLY these names (plus the ``LC_*`` prefix) are
#: forwarded to ``claude`` — the full parent env (GH_TOKEN, AWS_*, ANTHROPIC_API_KEY,
#: arbitrary secrets) is dropped so an autonomous agent running untrusted code
#: cannot read them. ``HOME`` is load-bearing: subscription auth reads
#: ``$HOME/.claude/.credentials.json`` (verified live with this exact trimmed env).
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TERM",
        "TZ",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_HOME",
        "SHELL",
        # Claude credential / config path overrides (subscription auth, NOT the
        # API key — ANTHROPIC_API_KEY is deliberately NOT here so a stray key in
        # the parent env never silently flips an autonomous agent to API billing).
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_CREDENTIALS_PATH",
    }
)

#: Names explicitly scrubbed even if they slip past the allowlist logic (secrets
#: an autonomous agent must never see). The allowlist already excludes them; this
#: is belt-and-suspenders documentation of the threat model.
ENV_DENYLIST_NEVER: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"})


def build_subprocess_env(
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal allowlisted env for the ``claude`` subprocess.

    Forwards ONLY :data:`ENV_ALLOWLIST` names (plus any ``LC_*`` locale vars) from
    ``parent_env`` (defaults to ``os.environ``). Everything else — secrets, tokens,
    cloud creds, ``ANTHROPIC_API_KEY`` — is dropped so an autonomous agent running
    untrusted code in the clone cannot exfiltrate them. ``ANTHROPIC_API_KEY`` is
    additionally never forwarded even if a future edit widens the allowlist (it
    would silently flip the run to API billing AND hand the key to the agent).
    """
    src = os.environ if parent_env is None else parent_env
    out: dict[str, str] = {}
    for name, value in src.items():
        if name in ENV_DENYLIST_NEVER:
            continue
        if name in ENV_ALLOWLIST or name.startswith("LC_"):
            out[name] = value
    return out


# ── Sandbox seam ────────────────────────────────────────────────────────────
#
# `sandbox_wrap(argv) -> argv` transforms the claude argv to run under an
# OS-level sandbox. Default is identity (UNSANDBOXED — a loud one-time warning
# fires). `bwrap_sandbox_wrap(clone_dir)` returns a wrapper that enables Claude's
# native bubblewrap sandbox confining writes to the clone, fail-closed.

SandboxWrap = Callable[[Sequence[str]], list[str]]

# Fires the unsandboxed warning at most once per process (it is an operator
# heads-up, not a per-dispatch nag).
_UNSANDBOXED_WARNED = False


def identity_sandbox_wrap(argv: Sequence[str]) -> list[str]:
    """The default (UNSANDBOXED) sandbox wrap: returns argv unchanged.

    Emits a prominent ONE-TIME warning that CLI transport is running without
    OS-level confinement. The path-guard + permission posture are defense-in-depth
    only; they do NOT contain an autonomous agent's writes (proven live).
    """
    global _UNSANDBOXED_WARNED
    if not _UNSANDBOXED_WARNED:
        _UNSANDBOXED_WARNED = True
        import logging

        logging.getLogger(__name__).warning(
            "ATELIER CLI TRANSPORT IS RUNNING UNSANDBOXED — OS-level write/network "
            "confinement is NOT enforced. The clone path-guard + acceptEdits + "
            "tool deny-list are defense-in-depth only; a live probe proved an "
            "agent can still write outside the clone without an OS sandbox. Wire a "
            "real sandbox via the sandbox_wrap seam (e.g. bwrap_sandbox_wrap) "
            "before relying on confinement. M7 (defaulting CLI transport ON) is "
            "BLOCKED until this seam carries a real sandbox."
        )
    return list(argv)


def bwrap_sandbox_wrap(clone_dir: str | os.PathLike[str]) -> SandboxWrap:
    """Return a ``sandbox_wrap`` that enables Claude's native bubblewrap sandbox.

    Injects ``--settings`` with a sandbox config that confines filesystem WRITES
    to ``clone_dir`` and sets ``failIfUnavailable=true`` — so on a host WITHOUT
    bubblewrap (``bwrap``) the ``claude`` CLI REFUSES TO START (verified live:
    "sandbox required but unavailable … refusing to start"), which is fail-closed
    (no uncontained agent ever runs). On a host WITH bwrap, writes outside the
    clone are blocked at the OS level.

    NOTE: this is the *Claude-native* sandbox. A future host may instead wrap argv
    with an external ``bwrap …``/container command; the seam accepts any
    ``argv -> argv`` transform. This wrapper is the batteries-included option.
    """
    clone_str = str(Path(clone_dir).resolve())

    def wrap(argv: Sequence[str]) -> list[str]:
        settings = json.dumps(
            {
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "filesystem": {"allowWrite": [clone_str]},
                    # No network egress by default (the design's "no net egress").
                    "network": {"allowedDomains": []},
                }
            }
        )
        return [*argv, "--settings", settings]

    return wrap


# ── Runner seam ────────────────────────────────────────────────────────────
#
# A `runner(argv, cwd) -> Awaitable[dict]` callable. The REAL runner
# (`real_cli_runner`) shells out to `claude` via `create_subprocess_exec` and
# parses the result JSON. Tests inject a `FakeCliRunner` so CI runs with NO
# `claude` invocation.

Runner = Callable[[Sequence[str], str], Awaitable[dict[str, Any]]]


async def real_cli_runner(argv: Sequence[str], cwd: str) -> dict[str, Any]:
    """Run ``claude`` as a subprocess and parse its single JSON result object.

    Uses ``asyncio.create_subprocess_exec`` with an argv LIST and NO ``shell`` —
    the safe form: every element (prompt, briefing, schema, model) is a discrete
    argument, never interpolated into a shell command, so no shell metacharacter
    is interpreted. The clone-escape path-guard in :func:`run_attempt` has already
    asserted ``cwd`` is inside the experiment clone before this is reached.

    Security:

    * **Minimal env.** The child receives ONLY :func:`build_subprocess_env`'s
      allowlist (PATH/HOME/USER/locale + credential-path vars) — NOT the full
      parent env. An autonomous agent running untrusted code never sees GH_TOKEN /
      cloud creds / ANTHROPIC_API_KEY. Subscription auth rides
      ``$HOME/.claude/.credentials.json`` (verified live with this trimmed env).
    * **Reap on cancel.** On cancellation/timeout (the ``asyncio.wait_for`` in
      :func:`run_attempt` trips) the ``CancelledError`` propagates through the
      ``finally``, which ``kill()``s and ``wait()``s the child so a hung ``claude``
      is genuinely terminated — never leaked as a zombie (the silently-dead-worker
      class). An orphaned ``acceptEdits`` agent would keep running with no gate.

    Returns the parsed result dict (``usage`` / ``structured_output`` /
    ``is_error`` / ``subtype`` / …). Raises on a non-zero exit or unparseable
    stdout — :func:`run_attempt` maps those to a failed attempt.
    """
    proc = await asyncio.create_subprocess_exec(  # nosec B603 — argv is a controlled LIST, no shell; prompt/briefing/schema are discrete argv items, never concatenated into a shell string; env is a minimal allowlist
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_subprocess_env(),
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except (asyncio.CancelledError, BaseException):
        # Cancelled (wall-clock timeout) or any failure mid-flight: REAP the child
        # before propagating so no orphaned `claude` survives the cancelled
        # coroutine. kill() is idempotent-safe; wait() collects the zombie.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr_b.decode('utf-8', 'replace')[:500]}"
        )
    return json.loads(stdout_b.decode("utf-8"))


# Marker: a runner that spawns a REAL, write-capable claude process. The
# mandatory-sandbox gate keys on this — a real runner with no sandbox is refused.
# A custom real runner SHOULD set ``runner.spawns_real_process = True`` (or be
# wrapped so this attribute is visible) to inherit the gate; the FakeCliRunner
# leaves it False so tests are exempt.
real_cli_runner.spawns_real_process = True  # type: ignore[attr-defined]


def _runner_spawns_real_process(runner: Runner) -> bool:
    """True iff *runner* spawns a real OS process (so the sandbox gate applies)."""
    return bool(getattr(runner, "spawns_real_process", False))


class FakeCliRunner:
    """A record/replay fake of the CLI runner for CI (NO ``claude`` invocation).

    Records every ``(argv, cwd)`` it is called with and returns a configurable
    canned result dict mirroring the verified ``claude --output-format json``
    shape (``usage`` / ``structured_output`` / ``is_error`` / ``subtype`` / …).

    Parameters
    ----------
    structured_output:
        The object returned under the result's ``structured_output`` key (the
        schema-validated envelope). Pass ``None`` to OMIT the key entirely (→ a
        failed attempt). A callable ``(argv, cwd) -> object`` is invoked per call
        for per-task results.
    usage:
        The ``usage`` block (defaults to a small non-zero output-token usage).
    is_error / subtype:
        Error signalling — ``is_error=True`` or ``subtype != "success"`` makes
        :func:`run_attempt` treat the result as a failed attempt.
    sleep:
        Optional seconds to ``await asyncio.sleep`` before returning — lets a
        test exercise the ``wait_for(WALL_CLOCK_S)`` timeout path.
    raise_exc:
        Optional exception to raise instead of returning (simulates a subprocess
        crash / non-zero exit → failed attempt).
    """

    def __init__(
        self,
        *,
        structured_output: Any = "__default__",
        usage: Mapping[str, Any] | None = None,
        is_error: bool = False,
        subtype: str = "success",
        sleep: float = 0.0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.structured_output = structured_output
        self.usage = dict(usage) if usage is not None else {"output_tokens": 7, "input_tokens": 3}
        self.is_error = is_error
        self.subtype = subtype
        self.sleep = sleep
        self.raise_exc = raise_exc
        #: Every call appended as ``{"argv": [...], "cwd": "..."}``.
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _structured_output_for(self, argv: Sequence[str], cwd: str) -> Any:
        so = self.structured_output
        if callable(so):
            return so(argv, cwd)
        if so == "__default__":
            # Derive a well-formed envelope from the argv so the default path
            # round-trips through validate_envelope. The task_id/attempt are
            # carried on the runner instance by run_attempt via the closure when
            # callers want exact matching; the default is a minimal `done`.
            return {
                "type": "task_result",
                "task_id": "t",
                "attempt": 1,
                "status": "done",
                "artifacts": [{"path": "f.py", "sha": "s"}],
                "notes_md": "done",
            }
        return so

    async def __call__(self, argv: Sequence[str], cwd: str) -> dict[str, Any]:
        self.calls.append({"argv": list(argv), "cwd": cwd})
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.raise_exc is not None:
            raise self.raise_exc
        result: dict[str, Any] = {
            "usage": dict(self.usage),
            "total_cost_usd": 0.0,
            "is_error": self.is_error,
            "subtype": self.subtype,
            "session_id": "fake-session",
            "num_turns": 1,
            "stop_reason": "end_turn",
        }
        so = self._structured_output_for(argv, cwd)
        if so is not None:
            result["structured_output"] = so
        return result


# ── R1 clone-escape guard ──────────────────────────────────────────────────


def _assert_inside_clone(candidate: str | os.PathLike[str], clone_real: Path, label: str) -> None:
    """Assert ``candidate`` resolves to a real path INSIDE ``clone_real``.

    Uses ``Path.resolve()`` (which normalizes ``..`` traversal and follows
    symlinks) then checks containment with ``Path.is_relative_to``. The clone
    itself counts as inside (a task may write at the clone root). Raises
    :class:`CloneEscapeError` on any escape — fail-closed, BEFORE any spawn.
    """
    resolved = Path(candidate).resolve()
    # is_relative_to(clone) is True for clone itself and any descendant.
    if not resolved.is_relative_to(clone_real):
        raise CloneEscapeError(f"{label}={candidate!s} (resolved {resolved})", str(clone_real))


def _default_est_for(model: str) -> int:
    """Cold-start per-agent output-token estimate by tier (idea 1/2 seed).

    Conservative constants until the journal accumulates real per-(phase,role)
    ``output_tokens``. Higher tiers get a larger estimate so the budget gate is
    proportionate. Unknown aliases fall back to the sonnet middle.
    """
    return {"haiku": 2_000, "sonnet": 6_000, "opus": 12_000}.get(model, 6_000)


# ── The one metered, validated, journaled attempt ──────────────────────────


async def run_attempt(
    task: Mapping[str, Any],
    attempt: int,
    *,
    budget: BudgetPool,
    journal: ResultJournal,
    model: str,
    briefing: str,
    clone_dir: str | os.PathLike[str],
    upstream_envelope_hashes: Sequence[str] | None = None,
    runner: Runner = real_cli_runner,
    add_dir: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    est_for: Callable[[str], int] = _default_est_for,
    wall_clock_s: float = WALL_CLOCK_S,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
    allowed_tools: Sequence[str] | None = None,
    sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
) -> dict[str, Any] | _FailedAttempt:
    """Run ONE metered, schema-validated, journaled ``claude -p`` attempt.

    Returns the validated envelope dict on success, or :data:`FAILED_ATTEMPT`
    (the engine routes it through ``_handle_failed_attempt``). Raises
    :class:`~scripts.budget_pool.BudgetExceeded` (pre-spawn, terminal),
    :class:`CloneEscapeError` (pre-spawn, refused), or
    :class:`UnsandboxedBypassError` (``bypassPermissions`` requested without a
    sandbox). See module docstring for the 7-step pipeline + security posture.

    ``cwd`` / ``add_dir`` default to ``clone_dir``; when supplied they MUST still
    resolve inside ``clone_dir`` (the R1 path-guard refuses otherwise BEFORE
    spawning). ``upstream_envelope_hashes`` is the DIRECT reads-from set (M1
    journal contract); compute it with :func:`direct_upstream_hashes`.

    Security args (default-secure):

    * ``permission_mode`` — ``"acceptEdits"`` by default; ``"bypassPermissions"``
      is REFUSED unless ``sandbox_wrap`` is non-identity (a real sandbox is wired).
    * ``disallowed_tools`` — ``("Bash", "WebFetch", "WebSearch")`` by default.
    * ``allowed_tools`` — optional explicit allowlist (e.g. Read/Edit/Write/Grep).
    * ``sandbox_wrap`` — ``argv -> argv`` OS-sandbox transform (default identity +
      one-time unsandboxed warning; see :func:`bwrap_sandbox_wrap`).
    """
    # INVARIANT: the adapter's per-attempt wall clock must not exceed the engine's
    # deadline, else a future lower engine WALL_CLOCK_S would be silently gated
    # behind this subprocess wait_for (contradicting the termination proof).
    assert wall_clock_s <= WALL_CLOCK_S, (
        f"adapter wall_clock_s={wall_clock_s} must be <= engine WALL_CLOCK_S="
        f"{WALL_CLOCK_S}; a longer subprocess deadline would defeat the engine's "
        "per-attempt bound."
    )

    # bypassPermissions is only safe inside a real sandbox — refuse it otherwise.
    if permission_mode == "bypassPermissions" and sandbox_wrap is identity_sandbox_wrap:
        raise UnsandboxedBypassError()

    up_hashes = list(upstream_envelope_hashes or [])

    # 1. Journal lookup — a HIT returns the cached envelope at $0 (no subprocess).
    key = journal.key(
        task,
        attempt,
        model=model,
        briefing=briefing,
        upstream_envelope_hashes=up_hashes,
    )
    hit = journal.lookup(key)
    if hit is not None:
        return hit

    # 2. Budget gate — raises BudgetExceeded BEFORE any subprocess (terminal).
    budget.assert_can_dispatch(est_for(model))

    # 3. R1 CLONE-ESCAPE GUARD — refuse BEFORE spawning if cwd / --add-dir escape.
    clone_real = Path(clone_dir).resolve()
    effective_cwd = clone_dir if cwd is None else cwd
    effective_add_dir = clone_dir if add_dir is None else add_dir
    _assert_inside_clone(effective_cwd, clone_real, "cwd")
    _assert_inside_clone(effective_add_dir, clone_real, "--add-dir")

    # 3b. MANDATORY-SANDBOX GATE (the escalation finding). A real, write-capable
    #     claude agent is NOT confined by the permission layer (proven live: an
    #     acceptEdits agent wrote outside the clone with empty permission_denials).
    #     So we REFUSE to spawn a real process unless a sandbox is wired
    #     (non-identity sandbox_wrap) OR the operator explicitly attests the host
    #     is already OS-confined (ATELIER_CLI_ALLOW_UNSANDBOXED=1). FakeCliRunner
    #     (no real process) is exempt. This fires only on a journal MISS + a real
    #     runner — a $0 journal hit above never reaches here.
    if (
        _runner_spawns_real_process(runner)
        and sandbox_wrap is identity_sandbox_wrap
        and os.environ.get(UNSANDBOXED_OPT_OUT_ENV) != "1"
    ):
        raise UnsandboxedRealRunError()

    # 4. Build argv as a LIST (never a shell string). prompt == briefing (the
    #    one-shot task envelope request rides the system prompt + the json-schema
    #    constraint; the -p positional carries the task request line). The
    #    permission posture (acceptEdits default), the tool deny-list, the optional
    #    allow-list, and the sandbox wrap are the write-confinement controls — the
    #    path-guard above is defense-in-depth only (it does NOT confine the agent).
    prompt = _prompt_for(task, attempt)
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(ENVELOPE_SCHEMA),
        "--model",
        model,
        "--system-prompt",
        briefing,
        "--permission-mode",
        permission_mode,
        "--add-dir",
        str(effective_add_dir),
    ]
    if disallowed_tools:
        argv += ["--disallowedTools", *disallowed_tools]
    if allowed_tools:
        argv += ["--allowedTools", *allowed_tools]
    # Apply the OS-sandbox wrap LAST (it appends --settings / wraps the command).
    argv = sandbox_wrap(argv)

    # 5. Run, bounded by the wall clock. Any failure → a failed attempt (NOT
    #    journaled). asyncio.TimeoutError, a non-zero exit (RuntimeError from the
    #    real runner), or unparseable stdout (json.JSONDecodeError) all collapse
    #    to the sentinel. On timeout the runner reaps the child (no zombie).
    try:
        result = await asyncio.wait_for(runner(argv, str(effective_cwd)), timeout=wall_clock_s)
    except TimeoutError:
        return _FailedAttempt("wall-clock timeout")
    except (RuntimeError, json.JSONDecodeError, ValueError, OSError) as exc:
        return _FailedAttempt(f"runner error: {exc}")

    failure = _failure_reason(result)
    if failure is not None:
        return _FailedAttempt(failure)

    # 6. Validate the envelope against the host's OWN dispatch identity (anti-spoof).
    structured = result["structured_output"]
    try:
        env = validate_envelope(
            structured,
            dispatched_task_id=task["task_id"],
            dispatched_attempt=attempt,
        )
    except Exception as exc:  # EnvelopeValidationError (and any malformed input)
        return _FailedAttempt(f"envelope validation failed: {exc}")

    # 7. Charge the meter + journal the success; return the validated envelope.
    usage = dict(result.get("usage") or {})
    budget.charge(usage)
    journal.put(key, env, usage=usage)
    return env


def _failure_reason(result: Mapping[str, Any]) -> str | None:
    """Return a failure reason string iff *result* is NOT a usable success.

    A usable success requires: ``is_error`` falsy AND ``subtype == "success"``
    AND a present ``structured_output``. Anything else → a failed attempt.
    """
    if result.get("is_error"):
        return f"is_error=True (subtype={result.get('subtype')!r})"
    if result.get("subtype") != "success":
        return f"subtype={result.get('subtype')!r} != 'success'"
    if "structured_output" not in result or result.get("structured_output") is None:
        return "missing structured_output"
    return None


def _prompt_for(task: Mapping[str, Any], attempt: int) -> str:
    """The ``-p`` positional: a terse task-envelope request line.

    The heavy lifting (persona, rules, task body, reply contract) is in the
    ``--system-prompt`` briefing; the ``-p`` prompt is the trigger that asks the
    worker to perform the task and emit its terminal envelope. Deterministic
    (clock/RNG-free) so the argv is stable for the journal-key-adjacent argv
    equality tests.
    """
    return (
        f"Perform task {task.get('task_id')} (attempt {attempt}) per your system "
        "prompt and emit ONLY the terminal task_result envelope matching the "
        "provided json-schema."
    )


# ── M1 journal host-driver contract helper ─────────────────────────────────


def direct_upstream_hashes(task_id: str, dag_proof: Any, journal: ResultJournal) -> frozenset[str]:
    """Resolve the DIRECT reads-from upstream ENVELOPE hashes for ``task_id``.

    The M1 ``ResultJournal`` host-driver contract: pass each task's DIRECT
    reads-from upstream envelope hashes (NOT a pre-expanded transitive closure —
    transitivity is achieved by content-chaining in the journal). This helper is
    the bridge from a :class:`~scripts.dag.DagProof` (whose ``reads_from`` gives
    the direct upstream task IDs) to the journal envelope hashes those tasks
    produced.

    M3 does NOT need the full scheduler that threads these through ``run_attempt``
    (that is M4) — but M4 just calls this. For each direct upstream task id, we
    look up its journal envelope hash; an upstream NOT yet in the journal (no
    completed attempt) contributes NO hash (it is silently skipped — the journal
    key naturally misses while an upstream is incomplete, which is correct: the
    task is not yet replayable).

    NOTE: this matches by raw ``task_id``. The host driver is responsible for
    having journaled each upstream under a key whose ``get_envelope_hash`` is
    discoverable; M4 wires the (task_id → journal key) map. M3 supplies a simple
    by-key resolution via an injected ``key_for`` is deliberately deferred — here
    we accept a ``journal`` that exposes ``get_envelope_hash`` keyed by the
    upstream's journal KEY, and the caller passes a journal whose keys ARE the
    upstream task journal keys. To keep M3 self-contained and unit-testable, the
    resolution is: for each upstream id, the caller must have stored that
    upstream's envelope hash retrievable via ``journal.get_envelope_hash`` under
    a key equal to the upstream's recorded journal key. Since M3 has no scheduler,
    the test exercises the relation by storing under the upstream id directly.

    Returns a ``frozenset[str]`` of envelope hashes (order-independent; the
    journal sorts before hashing).
    """
    hashes: set[str] = set()
    for up_id in dag_proof.reads_from(task_id):
        eh = journal.get_envelope_hash(up_id)
        if eh is not None:
            hashes.add(eh)
    return frozenset(hashes)


# ── CliDispatchTools — the DispatchTools Protocol, real version ─────────────


class _InFlight:
    """One in-flight attempt's future + its dispatch identity."""

    __slots__ = ("attempt", "future", "task")

    def __init__(self, task: Mapping[str, Any], attempt: int, future: asyncio.Future) -> None:
        self.task = task
        self.attempt = attempt
        self.future = future


class CliDispatchTools:
    """The real ``DispatchTools`` binding for the deterministic host (CLI mode).

    The analog of ``QueueBridgeDispatchTools``: instead of enqueueing a bridge
    request and servicing a queue, each dispatch launches ONE
    ``loop.create_task(run_attempt(...))`` on the owned loop and records the future
    in an in-flight map. The WaveDispatcher drives this through the
    :func:`build_cli_spawn_fn` / :func:`build_cli_poll_fn` seams (spawn fires the
    task; poll reads the future when ``done()``).

    Holds the per-cycle invariants: the ``BudgetPool``, the ``ResultJournal``,
    the clone dir, the runner, the per-task ``model_for`` / ``briefing_for``
    seams, and the security posture (``permission_mode`` / ``disallowed_tools`` /
    ``allowed_tools`` / ``sandbox_wrap``, threaded into every ``run_attempt``). It
    does NOT own the engine's control flow — it is the leaf adapter.

    Owns an asyncio event loop when none is supplied; use it as a context manager
    (``with CliDispatchTools(...) as tools:``) so the owned loop is closed
    deterministically (M4 does). A caller-supplied ``loop`` is left to the caller.
    """

    def __init__(
        self,
        *,
        budget: BudgetPool,
        journal: ResultJournal,
        clone_dir: str | os.PathLike[str],
        model_for: Callable[[Mapping[str, Any], int], str],
        briefing_for: Callable[[Mapping[str, Any], int], str],
        runner: Runner = real_cli_runner,
        upstream_hashes_for: Callable[[Mapping[str, Any], int], Sequence[str]] | None = None,
        wall_clock_s: float = WALL_CLOCK_S,
        loop: asyncio.AbstractEventLoop | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
        allowed_tools: Sequence[str] | None = None,
        sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
    ) -> None:
        self.budget = budget
        self.journal = journal
        self.clone_dir = clone_dir
        self.model_for = model_for
        self.briefing_for = briefing_for
        self.runner = runner
        self.upstream_hashes_for = upstream_hashes_for
        self.wall_clock_s = wall_clock_s
        self.permission_mode = permission_mode
        self.disallowed_tools = tuple(disallowed_tools)
        self.allowed_tools = tuple(allowed_tools) if allowed_tools is not None else None
        self.sandbox_wrap = sandbox_wrap
        # The event loop the futures live on. The synchronous WaveDispatcher
        # drives spawn/poll outside any running loop, so we own one explicitly
        # and pump it from `pump()` (wired to the engine's injected sleep_fn).
        # `_owns_loop` tracks whether WE created it: only an owned loop is closed
        # in `close()` / `__del__` (a caller-supplied loop is the caller's to
        # close), and only an owned loop's open-ness implies an FD leak if unclosed.
        self._owns_loop = loop is None
        self._loop = loop if loop is not None else asyncio.new_event_loop()
        #: task_id → _InFlight (the futures map the poll seam reads). The latest
        #: spawn for a task_id replaces the entry, so a re-spawn supersedes the
        #: prior attempt's future (the stale-attempt guard in `poll` keys on it).
        self._in_flight: dict[Any, _InFlight] = {}

    def _coro_for(self, task: Mapping[str, Any], attempt: int) -> Awaitable[Any]:
        up = list(self.upstream_hashes_for(task, attempt)) if self.upstream_hashes_for else []
        return run_attempt(
            task,
            attempt,
            budget=self.budget,
            journal=self.journal,
            model=self.model_for(task, attempt),
            briefing=self.briefing_for(task, attempt),
            clone_dir=self.clone_dir,
            upstream_envelope_hashes=up,
            runner=self.runner,
            wall_clock_s=self.wall_clock_s,
            permission_mode=self.permission_mode,
            disallowed_tools=self.disallowed_tools,
            allowed_tools=self.allowed_tools,
            sandbox_wrap=self.sandbox_wrap,
        )

    def spawn(self, task: Mapping[str, Any], attempt: int) -> None:
        """Launch ``run_attempt`` as an asyncio task into the in-flight map.

        The task is scheduled on this tools' own loop (``self._loop``) so the
        SYNCHRONOUS WaveDispatcher — which calls this outside any running loop —
        can drive it. Call :meth:`pump` (wired to the engine's ``sleep_fn``) to
        let the scheduled tasks make progress between polls.
        """
        task_id = task["task_id"]
        future = self._loop.create_task(self._coro_for(task, attempt))
        self._in_flight[task_id] = _InFlight(task, attempt, future)

    def pump(self) -> None:
        """Advance the event loop until every in-flight future has resolved.

        Wired to the WaveDispatcher's injected ``sleep_fn`` so each poll round
        that made no progress drains the scheduled ``run_attempt`` coroutines.
        Runs the loop to completion of the currently-pending futures — with the
        ``FakeCliRunner`` (no real subprocess) this resolves immediately; with a
        real runner it awaits the bounded ``claude`` calls. Resolved futures are
        left in place so :meth:`poll` can read their result.
        """
        pending = [infl.future for infl in self._in_flight.values() if not infl.future.done()]
        if not pending:
            return
        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def close(self) -> None:
        """Close the OWNED event loop (idempotent, no-op for a caller's loop).

        Cancels any still-pending futures + closes the loop we created so no
        selector FD / unclosed-socket leaks. A caller-supplied loop is left
        untouched (the caller owns its lifecycle). Use the context-manager form
        (``with CliDispatchTools(...) as tools:``) so this fires deterministically.
        """
        if not self._owns_loop:
            return
        if self._loop.is_closed():
            return
        # Cancel any leftover in-flight futures so closing the loop is clean.
        pending = [infl.future for infl in self._in_flight.values() if not infl.future.done()]
        for fut in pending:
            fut.cancel()
        if pending:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def __enter__(self) -> CliDispatchTools:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Belt-and-suspenders: close the owned loop if the caller forgot the
        # context manager / close(). Guarded so a half-constructed instance (no
        # _loop attribute yet) never raises in the finalizer.
        if getattr(self, "_owns_loop", False):
            with contextlib.suppress(Exception):
                self.close()

    def poll(self, task: Mapping[str, Any], attempt: int) -> Mapping[str, Any] | None:
        """Return the validated envelope iff the attempt's future is ``done()``.

        Stale-attempt guard: a poll for an attempt OTHER than the latest spawned
        attempt for this task returns ``None`` (the prior attempt's future is no
        longer the live one — a re-spawn replaced the in-flight entry, so polling
        the old attempt number must not read the new future).

        Returns ``None`` while the future is still running, on a failed-attempt
        sentinel (the engine then treats it as a silently-dead worker → soft-kill
        / re-queue), or when the future raised a terminal exception
        (``BudgetExceeded`` / ``CloneEscapeError`` are re-raised so the host
        surfaces them, not swallowed). Stability across re-polls comes from
        ``future.done()`` + idempotent ``future.result()`` — there is no result
        cache to keep in sync.
        """
        task_id = task["task_id"]
        infl = self._in_flight.get(task_id)
        if infl is None or infl.attempt != attempt:
            return None
        fut = infl.future
        if not fut.done():
            return None
        exc = fut.exception()
        if exc is not None:
            # Terminal/host errors propagate (budget, clone-escape); the engine
            # is not expected to absorb them as a failed attempt.
            raise exc
        result = fut.result()
        if is_failed_attempt(result):
            return None
        return result


def build_cli_spawn_fn(
    tools: CliDispatchTools,
) -> Callable[[Mapping[str, Any], int], None]:
    """Build the WaveDispatcher ``spawn_fn(task, attempt) -> None`` seam.

    Mirrors ``pm_dispatch.WaveDispatcher``'s ``spawn_fn`` contract EXACTLY
    (fire-and-forget; the reply is read back through the separate ``poll_fn``),
    so the engine drives the CLI transport unchanged — the same shape
    ``dispatch.build_spawn_fn`` produces for the bridge.
    """

    def spawn_fn(task: Mapping[str, Any], attempt: int) -> None:
        tools.spawn(task, attempt)

    return spawn_fn


def build_cli_poll_fn(
    tools: CliDispatchTools,
) -> Callable[[Mapping[str, Any], int], Mapping[str, Any] | None]:
    """Build the WaveDispatcher ``poll_fn(task, attempt) -> Mapping | None`` seam.

    Returns the future's validated envelope when ``done()``, else ``None`` — the
    exact non-blocking contract ``dispatch.build_poll_fn`` honors for the bridge,
    so the engine's GO-OBSERVE / single-re-queue logic is unchanged.
    """

    def poll_fn(task: Mapping[str, Any], attempt: int) -> Mapping[str, Any] | None:
        return tools.poll(task, attempt)

    return poll_fn


__all__ = [
    "DEFAULT_DISALLOWED_TOOLS",
    "DEFAULT_PERMISSION_MODE",
    "ENVELOPE_SCHEMA",
    "ENV_ALLOWLIST",
    "FAILED_ATTEMPT",
    "TRANSPORT_CLI",
    "UNSANDBOXED_OPT_OUT_ENV",
    "CliDispatchTools",
    "CloneEscapeError",
    "FakeCliRunner",
    "Runner",
    "SandboxWrap",
    "UnsandboxedBypassError",
    "UnsandboxedRealRunError",
    "build_cli_poll_fn",
    "build_cli_spawn_fn",
    "build_subprocess_env",
    "bwrap_sandbox_wrap",
    "direct_upstream_hashes",
    "identity_sandbox_wrap",
    "is_failed_attempt",
    "real_cli_runner",
    "run_attempt",
]
