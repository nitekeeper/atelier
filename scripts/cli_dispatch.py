"""CliDispatchTools — the real ``claude -p`` adapter (M3, behind a flag).

This is the production leaf of the deterministic-host engine: it turns ONE task
attempt into ONE **metered, schema-validated, journaled** ``claude -p`` call.
It is the real version of the M0 PoC ``run_attempt`` seam and the production
``DispatchTools`` binding for the host (CLI) transport.

The ``cli`` transport is the M7 default; the deterministic host is the only
dispatch path. M3 built the per-task adapter + the engine seams; the pipeline /
scheduler wiring is M4.

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
  runner) trips the mandatory-sandbox gate above. :func:`native_sandbox_wrap`
  (alias :func:`bwrap_sandbox_wrap`) enables **Claude Code's native sandbox** with
  ``filesystem.allowWrite=[clone]`` + ``network.allowedDomains=[]`` +
  ``failIfUnavailable=true`` — fail-closed: the CLI REFUSES TO START when the
  platform sandbox can't initialize (verified live), so an unconfined agent never
  runs. The injected ``--settings`` JSON is **cross-platform**: Claude implements
  the sandbox with **bubblewrap (``bwrap``) + ``socat`` on Linux/WSL2** and the
  **built-in Seatbelt framework on macOS** (zero installs). Availability detection
  is via :func:`sandbox_runtime_available` / :func:`sandbox_prereq_status` (Linux
  needs ``bwrap`` AND ``socat``; macOS is always available). **M7 (defaulting CLI
  transport ON) is BLOCKED until a real OS-level sandbox is wired into this seam**
  (on Linux: ``bubblewrap`` + ``socat`` installed; macOS: built-in; or an external
  container/namespace wrapper).
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
import shutil
import signal
import sys
import time
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
    real OS-level sandbox (Claude-native bubblewrap/Seatbelt, or an external
    container/restricted-user). With no sandbox, the host REFUSES it and falls back
    to (or demands) ``acceptEdits``.
    """

    def __init__(self) -> None:
        super().__init__(
            "permission_mode='bypassPermissions' refused: it disables all "
            "permission gates and a live probe proved it lets the agent write "
            "outside the clone. It is permitted ONLY when a sandbox is wired via "
            "the sandbox_wrap seam (e.g. native_sandbox_wrap). Use the default "
            "'acceptEdits' instead, or wire a real OS-level sandbox."
        )


class UnsandboxedRealRunError(RuntimeError):
    """Raised when a REAL ``claude`` subprocess would be spawned with NO sandbox.

    **The escalation finding (proven live):** neither ``acceptEdits`` nor
    ``bypassPermissions`` confines writes to the clone — a real agent under either
    mode wrote OUTSIDE the clone with ``permission_denials: []``. The CLI
    permission layer is therefore NOT a containment boundary. So the host REFUSES
    to spawn a real, write-capable agent unless a real OS-level sandbox is wired
    via ``sandbox_wrap`` (e.g. :func:`native_sandbox_wrap`, which fails closed when
    the platform sandbox can't initialize — on Linux that means ``bwrap``/``socat``
    absent; on macOS Seatbelt is built-in).

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
            "(e.g. native_sandbox_wrap(clone_dir)); or, ONLY if the whole host is "
            f"already OS-confined, set {UNSANDBOXED_OPT_OUT_ENV}=1 to opt out. "
            f"{_sandbox_install_hint()} "
            "M7 (defaulting CLI transport ON) stays BLOCKED until the sandbox "
            "seam carries a real OS sandbox."
        )


#: Operator opt-out for the mandatory-sandbox gate. Set to ``"1"`` ONLY when the
#: entire host is already OS-confined (throwaway container/VM) so the in-process
#: sandbox is redundant. OFF by default — the gate is fail-closed.
UNSANDBOXED_OPT_OUT_ENV = "ATELIER_CLI_ALLOW_UNSANDBOXED"


# ── Platform-aware sandbox-runtime detection ───────────────────────────────
#
# Claude Code's native sandbox (the one we inject via `--settings` in
# `native_sandbox_wrap`) is implemented per-platform — the SAME settings schema
# drives BOTH (verified against code.claude.com/docs/en/sandboxing):
#
#   * macOS  → built-in **Seatbelt** framework. ZERO installs; `sandbox-exec`
#              ships with macOS. So the runtime is ALWAYS available on darwin.
#   * Linux/ → **bubblewrap (`bwrap`)** for filesystem isolation PLUS **`socat`**
#     WSL2     for the network proxy relay (docs: "the relay used to route network
#              traffic through the sandbox proxy"). We configure BOTH a filesystem
#              confinement (`filesystem.allowWrite`) AND a network policy
#              (`network.allowedDomains: []`), so BOTH packages are genuine
#              prerequisites — `socat` is NOT optional for our config (it is one of
#              the two packages the docs require, and `/sandbox`'s Dependencies tab
#              checks for it alongside `bwrap`).
#   * other  → native Windows is unsupported; anything else is unknown. Be
#              CONSERVATIVE → report unavailable (the gate then fail-closes).
#
# This is DETECTION + MESSAGING only. It does NOT relax the fail-closed
# mandatory-sandbox gate — `native_sandbox_wrap` still sets `failIfUnavailable`,
# so even a false "available" here cannot let an unconfined agent run (claude
# refuses to start if its sandbox can't init).


def sandbox_prereq_status() -> tuple[bool, str]:
    """Return ``(available, human_reason)`` for the native sandbox on THIS host.

    Platform-aware (see the module's platform table):

    * ``darwin`` → ``(True, …Seatbelt is built-in…)`` — no installs needed.
    * ``linux`` (incl. WSL2 — WSL2 reports as ``linux``) → available iff BOTH
      ``bwrap`` (bubblewrap, filesystem isolation) AND ``socat`` (the network-proxy
      relay) are on PATH; the reason names whichever is missing.
    * ``win32`` (native Windows) → ``(False, …run under WSL2…)`` — Claude Code's
      native sandbox does not support native Windows; the message points the
      operator at WSL2 (which reports as ``linux`` and uses the bwrap+socat path).
    * anything else (unknown) → ``(False, …unsupported…)`` — conservative: an
      unknown platform is treated as having no sandbox runtime.

    The boolean drives test skips + message composition; it does NOT relax the
    fail-closed gate (``native_sandbox_wrap`` always sets ``failIfUnavailable``).
    """
    platform = sys.platform
    if platform == "darwin":
        return (
            True,
            "macOS: native sandbox uses the built-in Seatbelt framework "
            "(sandbox-exec) — zero installs required; the runtime is always "
            "available.",
        )
    if platform == "win32":
        return (
            False,
            "Native Windows has no OS sandbox for autonomous agents — Claude "
            "Code's native sandbox supports only macOS, Linux, and WSL2. Run "
            "atelier under WSL2 (Windows Subsystem for Linux), where it reports as "
            "`linux` and uses bubblewrap + socat for confinement.",
        )
    if platform.startswith("linux"):
        has_bwrap = shutil.which("bwrap") is not None
        has_socat = shutil.which("socat") is not None
        if has_bwrap and has_socat:
            return (
                True,
                "Linux/WSL2: bubblewrap (bwrap) and socat are both present — the "
                "native sandbox runtime is available.",
            )
        missing = [
            name
            for name, present in (("bubblewrap (bwrap)", has_bwrap), ("socat", has_socat))
            if not present
        ]
        return (
            False,
            "Linux/WSL2: native sandbox unavailable — missing "
            + " and ".join(missing)
            + ". Install both with e.g. `sudo apt install bubblewrap socat` "
            "(bwrap enforces filesystem isolation; socat relays the network "
            "proxy used by network.allowedDomains).",
        )
    return (
        False,
        f"platform {platform!r}: native sandbox runtime is unavailable/unknown "
        "(native Windows is unsupported — run inside WSL2; any other platform is "
        "treated conservatively as having no sandbox).",
    )


def sandbox_runtime_available() -> bool:
    """True iff the native sandbox runtime is available on THIS platform.

    Thin boolean wrapper over :func:`sandbox_prereq_status`. ``darwin`` → always
    True (Seatbelt built-in); ``linux`` → True iff ``bwrap`` AND ``socat`` are on
    PATH; any other platform → False (conservative). Used by the live e2e harness
    skipif so a Mac with zero installs RUNS the test while a Linux host lacking
    ``bwrap``/``socat`` SKIPS with a platform-correct reason.
    """
    available, _reason = sandbox_prereq_status()
    return available


def _sandbox_install_hint() -> str:
    """Platform-aware remediation hint for the unsandboxed error/warning text.

    On Linux: name the two packages to install. On macOS: Seatbelt is built-in, so
    a missing sandbox means it failed to INITIALIZE (not a missing install). On an
    unknown platform: state it conservatively. Composed from
    :func:`sandbox_prereq_status` so the wording stays single-sourced.
    """
    platform = sys.platform
    if platform == "darwin":
        return (
            "On macOS the Seatbelt sandbox is built-in (zero installs) — if the "
            "sandbox is reported unavailable, it FAILED TO INITIALIZE rather than "
            "being absent; check the claude sandbox diagnostics (`/sandbox`)."
        )
    if platform.startswith("linux"):
        return (
            "On Linux/WSL2 install bubblewrap + socat, e.g. "
            "`sudo apt install bubblewrap socat` (bwrap = filesystem isolation, "
            "socat = the network-proxy relay)."
        )
    if platform == "win32":
        return (
            "Native Windows has no OS sandbox — run atelier under WSL2 (Windows "
            "Subsystem for Linux), where it uses bubblewrap + socat."
        )
    # Unknown platform — surface the conservative status verbatim.
    return sandbox_prereq_status()[1]


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

#: BUG3 — Default tool ALLOW-list. In headless ``-p`` mode a worker that has not
#: been granted a tool hits "requested permissions … haven't granted it yet" and
#: cannot Read/Edit — so the leaf builds ``--allowedTools`` with this set by
#: default. It carries the file/work primitives a worker needs (Read/Edit/Write/
#: MultiEdit/Grep/Glob/LS/Task/NotebookEdit/TodoWrite) but DELIBERATELY OMITS the
#: three deny-floor tools (Bash/WebFetch/WebSearch): :data:`DEFAULT_DISALLOWED_TOOLS`
#: is the floor and DENY WINS — a tool listed in both is still denied by claude.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Grep",
    "Glob",
    "LS",
    "Task",
    "NotebookEdit",
    "TodoWrite",
)

#: M5 (6) — transient-spawn retry bound INSIDE one charged engine attempt.
#: ``run_attempt`` retries ONLY a clearly-transient launch error (``OSError`` on
#: spawn — e.g. EAGAIN / ENOMEM / transient fork failure), up to this many EXTRA
#: tries (so ``TRANSIENT_SPAWN_RETRIES + 1`` total launch attempts). The retry
#: happens within the SINGLE ``asyncio.wait_for(wall_clock_s)`` TOTAL bound (never
#: a fresh deadline per try — that would multiply the per-attempt bound and defeat
#: the termination proof), the engine attempt number is untouched, and the budget
#: is charged exactly once (on the eventual success). A non-zero ``claude`` exit
#: (``RuntimeError``), a wall-clock ``TimeoutError``, and JSON/Value errors are all
#: TERMINAL — the model ran and failed, so they charge the attempt and are NOT
#: retried as transient spawns.
TRANSIENT_SPAWN_RETRIES = 2
#: Tiny backoff between transient-spawn retries (seconds). Counts AGAINST the one
#: ``wall_clock_s`` total bound (it runs inside the ``wait_for``), so it is kept
#: small; tests monkeypatch it to 0.
_TRANSIENT_SPAWN_BACKOFF_S = 0.05

#: M5 (7) — ``--max-budget-usd`` derivation constants. The BudgetPool gates on
#: OUTPUT tokens; the documented ``claude --max-budget-usd`` flag is a DOLLAR cost
#: ceiling ("Maximum dollar amount to spend on API calls; only works with
#: --print"). We derive a per-task dollar ceiling from the per-task output-token
#: estimate the budget gate already computes, priced at the maintainer's default
#: roster model (Opus) rate, plus an input-token allowance, with the SAME headroom
#: factor the budget pool applies — so the dollar lever is proportionate to the
#: token lever, never tighter than the work actually needs.
#:
#: Pricing source (claude-api skill, cached 2026-06-04): Opus-tier output is
#: $25.00 / 1M tokens, input $5.00 / 1M tokens. Using the priciest roster tier
#: makes the ceiling conservative (never under-budgets a cheaper-tier task).
_USD_PER_OUTPUT_TOKEN = 25.0 / 1_000_000  # Opus output: $25 / MTok
_USD_PER_INPUT_TOKEN = 5.0 / 1_000_000  # Opus input: $5 / MTok
#: Assumed input:output token ratio for the dollar derivation. A task's input
#: (system prompt + briefing + upstream envelopes + tool I/O) typically dwarfs its
#: output; 5x is a deliberately generous allowance so the dollar lever never trips
#: BEFORE the wall-clock kill (the primary, correctness-guaranteeing lever).
_USD_INPUT_TO_OUTPUT_RATIO = 5.0
#: Multiplier applied to the priced estimate to form the dollar ceiling — slack so
#: the cost lever is a DEFENSE-IN-DEPTH backstop, not a primary gate (the budget
#: pool's output-token gate + the wall-clock kill are the primary stops). 1.0 /
#: 0.70 mirrors the budget pool's 0.70 headroom inverted: the dollar ceiling is the
#: token estimate grossed up by the same buffer the pool reserves.
_USD_CEILING_HEADROOM = 1.0 / 0.70

#: BUG2 — dollar FLOOR for the derived ``--max-budget-usd`` ceiling. The
#: token-estimate derivation above is tiny (an opus 12k-token task derives only
#: ~$0.86), so a BIG real task that legitimately runs long would trip the dollar
#: ceiling and abort the subprocess mid-run (``claude exited 1``). The dollar
#: lever is meant to be a DEFENSE-IN-DEPTH backstop — the wall-clock kill is the
#: REAL terminator — so we never let the derived ceiling fall below a workable
#: floor. ``max_budget_usd_for`` returns ``max(<derived>, MIN_BUDGET_USD)``;
#: because the derivation is monotone, the floor only ever clamps the SMALL
#: estimates up, never makes the ceiling unbounded (a task whose derived value
#: already exceeds the floor keeps its larger, proportionate ceiling).
MIN_BUDGET_USD = 5.0


def max_budget_usd_for(est_output_tokens: int) -> float:
    """Derive the per-task ``--max-budget-usd`` dollar ceiling from the per-task
    OUTPUT-token estimate the budget gate already computes (M5 change 7).

    The BudgetPool gates on output tokens; ``claude --max-budget-usd`` is a dollar
    cost ceiling. We price *est_output_tokens* at the Opus output rate, add an
    input-token allowance (``_USD_INPUT_TO_OUTPUT_RATIO`` x the output estimate at
    the input rate), and gross the sum up by ``_USD_CEILING_HEADROOM`` (the inverse
    of the pool's 0.70 headroom) so the dollar lever is proportionate to — and
    never tighter than — the token lever. Monotone in *est_output_tokens*: a bigger
    task gets a bigger ceiling. The amount is a DEFENSE-IN-DEPTH backstop; the
    wall-clock kill + child reap is the lever that guarantees termination.

    BUG2: the bare derivation is tiny (opus 12k → ~$0.86), so a big real task that
    legitimately runs long would trip the dollar ceiling and abort mid-run. We
    therefore clamp the result UP to :data:`MIN_BUDGET_USD` — ``max(<derived>,
    MIN_BUDGET_USD)`` — so the ceiling can never fall below a workable amount. The
    floor only raises the SMALL estimates (the derivation is monotone), keeping the
    ceiling proportionate and bounded: a task whose derived value already exceeds
    the floor keeps its larger value.
    """
    est = max(0, int(est_output_tokens))
    output_cost = est * _USD_PER_OUTPUT_TOKEN
    input_cost = est * _USD_INPUT_TO_OUTPUT_RATIO * _USD_PER_INPUT_TOKEN
    derived = (output_cost + input_cost) * _USD_CEILING_HEADROOM
    return max(derived, MIN_BUDGET_USD)


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
# fires). `native_sandbox_wrap(clone_dir)` (back-compat alias `bwrap_sandbox_wrap`)
# returns a wrapper that enables Claude Code's native sandbox confining writes to
# the clone, fail-closed. The injected `--settings` JSON is cross-platform: claude
# implements it with bubblewrap+socat on Linux/WSL2 and built-in Seatbelt on macOS.

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
            "real sandbox via the sandbox_wrap seam (e.g. native_sandbox_wrap) "
            "before relying on confinement. %s M7 (defaulting CLI transport ON) is "
            "BLOCKED until this seam carries a real sandbox.",
            _sandbox_install_hint(),
        )
    return list(argv)


def native_sandbox_wrap(
    clone_dir: str | os.PathLike[str],
    *,
    write_root: str | os.PathLike[str] | None = None,
) -> SandboxWrap:
    """Return a ``sandbox_wrap`` that enables **Claude Code's native sandbox**.

    Injects ``--settings`` with a sandbox config that confines filesystem WRITES
    to ``write_root`` (defaulting to ``clone_dir`` for back-compat), denies all
    network egress (``network.allowedDomains: []``), and sets
    ``failIfUnavailable=true`` — so on a host where the platform sandbox can't
    initialize the ``claude`` CLI REFUSES TO START (verified live: "sandbox
    required but unavailable … refusing to start"), which is fail-closed (no
    uncontained agent ever runs). On a host with a working sandbox, writes outside
    the write root are blocked at the OS level.

    BUG4a — ``write_root`` is the directory the agent is allowed to WRITE into.
    Workers run in a per-writer git worktree under
    ``clone_dir/.atelier-worktrees/<id>``, NOT the clone root, so a writer's
    sandbox must confine writes to (and land them in) ITS worktree — else the
    OS sandbox would either block the legitimate worktree write or (when pinned to
    the clone) let a write escape the worktree into a sibling's tree. The host
    therefore passes ``write_root=<the carved worktree path>`` for an isolated
    writer; a read-only / no-worktree task keeps the default ``clone_dir``. The
    parameter is keyword-only and defaults to ``clone_dir`` so every existing
    caller is unchanged.

    **Cross-platform** — the SAME ``--settings`` JSON drives both platforms; only
    the OS primitive differs (verified against code.claude.com/docs/en/sandboxing):

    * **Linux/WSL2** — Claude uses **bubblewrap (``bwrap``)** for filesystem
      isolation + **``socat``** for the network-proxy relay. BOTH packages are
      prerequisites (``sudo apt install bubblewrap socat``); availability is
      detected by :func:`sandbox_runtime_available`.
    * **macOS** — Claude uses the **built-in Seatbelt** framework; ZERO installs.

    So ``wrap()`` itself is platform-agnostic (it only appends ``--settings``);
    platform-awareness lives in the DETECTION helpers + the error/warning text.
    A future host may instead wrap argv with an external ``bwrap …``/container
    command; the seam accepts any ``argv -> argv`` transform. This wrapper is the
    batteries-included option.
    """
    # BUG4a: confine writes to write_root (the carved worktree when supplied),
    # falling back to clone_dir for the back-compat / no-worktree case.
    clone_str = str(Path(clone_dir).resolve())
    write_str = str(Path(write_root if write_root is not None else clone_dir).resolve())

    def wrap(argv: Sequence[str]) -> list[str]:
        settings = json.dumps(
            {
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "filesystem": {"allowWrite": [write_str]},
                    # No network egress by default (the design's "no net egress").
                    "network": {"allowedDomains": []},
                }
            }
        )
        return [*argv, "--settings", settings]

    # BUG4a: tag the closure so a per-writer caller (host_scheduler.pipeline) can
    # DETECT a native sandbox and rebuild it with the carved worktree as the
    # ``write_root`` (an opaque ``argv -> argv`` seam is otherwise un-introspectable).
    # ``native_clone_dir`` is the resolved clone; ``native_write_root`` is what THIS
    # wrap currently confines writes to.
    wrap.native_clone_dir = clone_str  # type: ignore[attr-defined]
    wrap.native_write_root = write_str  # type: ignore[attr-defined]
    return wrap


#: Back-compat alias. The wrapper was historically named ``bwrap_sandbox_wrap``
#: (Linux-only framing); it is now :func:`native_sandbox_wrap` to reflect the
#: cross-platform reality (bubblewrap+socat on Linux, Seatbelt on macOS — same
#: ``--settings`` JSON). The old name remains a callable alias so existing callers
#: (and any external code) keep working unchanged.
bwrap_sandbox_wrap = native_sandbox_wrap


# ── Runner seam ────────────────────────────────────────────────────────────
#
# A `runner(argv, cwd) -> Awaitable[dict]` callable. The REAL runner
# (`real_cli_runner`) shells out to `claude` via `create_subprocess_exec` and
# parses the result JSON. Tests inject a `FakeCliRunner` so CI runs with NO
# `claude` invocation.

Runner = Callable[[Sequence[str], str], Awaitable[dict[str, Any]]]


#: Grace, in seconds, between the SIGTERM and the SIGKILL escalation when reaping
#: a timed-out child's process group. Short on purpose: a hung ``claude`` worker
#: that ignores SIGTERM is force-killed quickly, while a cooperative child that
#: honours SIGTERM exits within the grace and is reaped immediately (the grace is
#: polled, not slept-through). Bounded so the reap completes well inside the test's
#: post-timeout settle window and the engine's deadline budget.
_TERM_GRACE_S = 0.5


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` still exists (signal 0 probe). A reaped/absent pid is dead."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        return False
    except PermissionError:
        # Exists but not ours to signal — treat as alive (conservative).
        return True
    return True


async def _reap_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate + reap the whole process group of a timed-out/cancelled child.

    The child is spawned with ``start_new_session=True`` so it leads its own
    session/process group; killing the GROUP (``os.killpg``) reaps any grandchild
    (e.g. a shell's ``sleep``) that a bare ``proc.kill()`` (direct child only)
    would orphan. Escalation is SIGTERM → polled grace → SIGKILL, then a
    SYNCHRONOUS, non-cancellable ``os.waitpid`` collects the zombie — critical
    because the ambient ``asyncio.wait_for`` cancellation can abort an
    ``await proc.wait()`` mid-flight (the prior bug: the SIGKILL had not yet
    landed, the cancelled wait was suppressed, and the child leaked).

    Idempotent and fail-soft: a child that already exited is a no-op; every
    ``os`` call tolerates a racing exit (``ProcessLookupError`` / ``ChildProcessError``).
    """
    if proc.returncode is not None:
        return
    pid = proc.pid

    def _signal_group(sig: int) -> None:
        # Prefer the whole process group (reaps grandchildren); fall back to the
        # direct pid if the group is already gone or pgid lookup races.
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, ChildProcessError):
            return
        except OSError:
            with contextlib.suppress(ProcessLookupError, ChildProcessError):
                os.kill(pid, sig)

    _signal_group(signal.SIGTERM)

    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError):
            if os.waitpid(pid, os.WNOHANG)[0] != 0:
                break
        if not _pid_alive(pid):
            break
        # Shielded so the ambient cancellation can't abort the grace poll.
        with contextlib.suppress(BaseException):
            await asyncio.shield(asyncio.sleep(0.01))

    if _pid_alive(pid):
        _signal_group(signal.SIGKILL)

    # Synchronous reap of the direct child — NOT cancellable, so the zombie is
    # always collected even while the coroutine is being torn down.
    with contextlib.suppress(ChildProcessError, ProcessLookupError):
        os.waitpid(pid, 0)
    # Let the asyncio transport observe the exit too (best-effort, shielded).
    with contextlib.suppress(BaseException):
        await asyncio.shield(proc.wait())


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
    * **Reap on cancel.** The child is spawned with ``start_new_session=True`` so
      it leads its own process group. On cancellation/timeout (the
      ``asyncio.wait_for`` in :func:`run_attempt` trips) the ``CancelledError``
      propagates through the ``except``, which reaps the whole GROUP via
      :func:`_reap_process_group` (SIGTERM → polled grace → SIGKILL, then a
      synchronous ``os.waitpid``). Killing the group — not just the direct child —
      reaps any grandchild a hung ``claude`` spawned, and the synchronous waitpid
      collects the zombie even while the coroutine is torn down. A hung ``claude``
      is genuinely terminated — never leaked as an orphan or zombie (the
      silently-dead-worker class). An orphaned ``acceptEdits`` agent would keep
      running with no gate.

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
        start_new_session=True,  # own process group → group-kill reaps grandchildren on timeout
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except BaseException:
        # Cancelled (wall-clock timeout) or any failure mid-flight: REAP the whole
        # process group before propagating so no orphaned `claude` (or grandchild)
        # survives the cancelled coroutine, and no zombie is left behind.
        await _reap_process_group(proc)
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr_b.decode('utf-8', 'replace')[:500]}"
        )
    return json.loads(stdout_b.decode("utf-8"))


# Marker polarity is FAIL-CLOSED (security #0, M4): the mandatory-sandbox gate
# treats EVERY runner as REAL (→ gated) by default and a runner must EXPLICITLY
# opt OUT of realness to be exempt. A runner that forgets to mark itself is
# therefore GATED (refused without a sandbox), never silently exempt. This
# inverts the prior fail-OPEN keying on a positive ``spawns_real_process`` flag —
# a real-spawning runner that forgot the marker would have been exempt.
#
# The exemption marker is ``no_real_process`` (aka ``is_fake``): set True ONLY on
# a runner that spawns no OS process (e.g. :class:`FakeCliRunner`). The positive
# ``spawns_real_process = True`` is RETAINED on ``real_cli_runner`` as a
# belt-and-suspenders explicit signal, but it is no longer load-bearing: an
# UNMARKED runner is already treated as real.
real_cli_runner.spawns_real_process = True  # type: ignore[attr-defined]


def _runner_spawns_real_process(runner: Runner) -> bool:
    """True iff the sandbox gate must apply to *runner* (it may spawn a real
    OS process).

    FAIL-CLOSED: a runner is considered real (→ gated) UNLESS it EXPLICITLY
    attests that it spawns no real process via ``no_real_process`` / ``is_fake``
    (set True). An unmarked/forgotten runner is treated as real and gated — the
    inverse of the prior keying on a positive ``spawns_real_process`` marker,
    which failed OPEN when a real runner forgot to set it.

    An explicit ``spawns_real_process = False`` does NOT exempt a runner on its
    own (it could be a forgotten default); only an affirmative fake marker
    (``no_real_process``/``is_fake`` True) exempts. This keeps the gate
    fail-closed against silent omissions.
    """
    # Real (→ gated) UNLESS the runner affirmatively attests it is a fake.
    return not (getattr(runner, "no_real_process", False) or getattr(runner, "is_fake", False))


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

    #: Explicit FAIL-CLOSED exemption marker (security #0): this runner spawns NO
    #: real OS process, so the mandatory-sandbox gate must NOT apply. The gate
    #: (:func:`_runner_spawns_real_process`) treats every runner as real UNLESS
    #: it sets this True — so a subclass that DOES spawn a real process (or wants
    #: to exercise the gate) must override it to False (see the security tests'
    #: ``_FakeRealRunner``).
    no_real_process: bool = True
    #: Alias accepted by the gate, for callers preferring ``is_fake`` semantics.
    is_fake: bool = True

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
    allowed_tools: Sequence[str] | None = DEFAULT_ALLOWED_TOOLS,
    sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
    max_budget_usd: float | None = None,
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
    * ``allowed_tools`` — explicit allowlist; defaults to
      :data:`DEFAULT_ALLOWED_TOOLS` (Read/Edit/Write/MultiEdit/Grep/Glob/LS/Task/
      NotebookEdit/TodoWrite) so a headless ``-p`` worker is granted the file/work
      primitives up-front (BUG3 — else it hits "haven't granted it yet"). The
      deny-floor still wins (Bash/WebFetch/WebSearch denied). Pass ``None`` / an
      empty sequence to omit ``--allowedTools`` entirely.
    * ``sandbox_wrap`` — ``argv -> argv`` OS-sandbox transform (default identity +
      one-time unsandboxed warning; see :func:`native_sandbox_wrap`).
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
    # M5 (7): the second hung-query kill lever — a DOCUMENTED, cost-model-aligned
    # dollar ceiling derived from the per-task output-token estimate (the same est
    # the budget gate uses), with the budget pool's headroom applied. This is
    # defense-in-depth; the wall-clock `wait_for` + child reap below is the lever
    # that GUARANTEES termination. `--max-budget-usd` is documented as only working
    # with `--print`, which we use (`-p`). Injectable via *max_budget_usd*; default
    # derives from est_for(model). NOTE: `--max-turns` is deliberately NOT wired —
    # it is undocumented/hidden on the pinned CLI (INERT-lever risk).
    budget_usd = (
        max_budget_usd_for(est_for(model)) if max_budget_usd is None else float(max_budget_usd)
    )
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
        "--max-budget-usd",
        f"{budget_usd:.2f}",
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
    #
    #    M5 (6): a clearly-transient LAUNCH error (OSError on spawn — EAGAIN /
    #    ENOMEM / transient fork failure) is retried INSIDE this one charged engine
    #    attempt, up to TRANSIENT_SPAWN_RETRIES extra tries with a tiny backoff.
    #    The attempt count is untouched and the budget is charged exactly once (on
    #    the eventual success, in step 7). CRITICAL: the ENTIRE retry loop runs
    #    inside the SINGLE `asyncio.wait_for(wall_clock_s)` TOTAL bound below — NOT
    #    a fresh wall_clock_s per try — so the per-attempt termination bound (and
    #    the :796-800 adapter<=engine invariant) is preserved. A non-zero exit
    #    (RuntimeError), a wall-clock TimeoutError, and JSON/Value errors are all
    #    TERMINAL: the model ran and failed, so they charge the attempt and are NOT
    #    retried as transient spawns.
    async def _launch_with_transient_retry() -> dict[str, Any]:
        last_os_err: OSError | None = None
        for try_i in range(TRANSIENT_SPAWN_RETRIES + 1):
            try:
                return await runner(argv, str(effective_cwd))
            except OSError as exc:
                # Clearly-transient launch failure — retry within the SAME
                # wall_clock_s total bound (the backoff counts against it too).
                last_os_err = exc
                if try_i < TRANSIENT_SPAWN_RETRIES:
                    if _TRANSIENT_SPAWN_BACKOFF_S:
                        await asyncio.sleep(_TRANSIENT_SPAWN_BACKOFF_S)
                    continue
                raise
        # Unreachable (the loop either returns or raises), but keep mypy happy.
        raise (
            last_os_err
            if last_os_err is not None
            else RuntimeError("spawn retry loop fell through")
        )

    try:
        result = await asyncio.wait_for(_launch_with_transient_retry(), timeout=wall_clock_s)
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

    No queue: each dispatch launches ONE
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
        allowed_tools: Sequence[str] | None = DEFAULT_ALLOWED_TOOLS,
        sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
        review_pairing: Mapping[str, str] | None = None,
        task_index: Mapping[Any, Mapping[str, Any]] | None = None,
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
        # M6b-1 — dispatch-time reviewer-disjointness re-check (leaf/parallel path).
        # ``review_pairing`` is the bidirectional implement↔review id map
        # (planner.build_review_pairing); ``task_index`` resolves a task_id → its
        # task dict. When a dispatched task is a paired REVIEWER, `_coro_for`
        # re-asserts separation-of-duties via the SAME comparator the planner used.
        # Both default empty ⇒ NO-OP for the parallel path unless explicitly wired.
        self.review_pairing = dict(review_pairing) if review_pairing else {}
        self.task_index = dict(task_index) if task_index else {}
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
        # M6b-1 (T6) — DISPATCH-TIME reviewer-disjointness re-check on the leaf/
        # parallel path. When THIS task is a paired REVIEWER (it carries a `reviews`
        # field AND the pairing is wired), re-assert separation-of-duties via the
        # SAME comparator the planner used at synthesis (one comparator, no drift):
        # a persona re-assigned to the implementer's AFTER synthesis is caught here.
        # Fail-LOUD — the raise propagates out of the spawned future and is surfaced
        # by the engine's poll seam (it never silently dispatches a self-review).
        reviews = task.get("reviews")
        if self.review_pairing and isinstance(reviews, str) and reviews:
            reviewed = self.task_index.get(reviews)
            if reviewed is not None:
                from scripts.planner import assert_reviewer_disjoint

                assert_reviewer_disjoint(dict(task), dict(reviewed))
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
    so the engine drives the CLI transport with the standard engine seam shape.
    """

    def spawn_fn(task: Mapping[str, Any], attempt: int) -> None:
        tools.spawn(task, attempt)

    return spawn_fn


def build_cli_poll_fn(
    tools: CliDispatchTools,
) -> Callable[[Mapping[str, Any], int], Mapping[str, Any] | None]:
    """Build the WaveDispatcher ``poll_fn(task, attempt) -> Mapping | None`` seam.

    Returns the future's validated envelope when ``done()``, else ``None`` — the
    standard non-blocking engine ``poll_fn`` contract, so the engine's GO-OBSERVE
    / single-re-queue logic is unchanged.
    """

    def poll_fn(task: Mapping[str, Any], attempt: int) -> Mapping[str, Any] | None:
        return tools.poll(task, attempt)

    return poll_fn


# ── M6a: CLI dispatch factory (production constructor for the host path) ──
#
# `_default_model_for(phase, env)` is the shared `scripts.model_tier.recommend`
# -backed tier closure. The host/CLI path previously had NO factory:
# `CliDispatchTools` took `model_for` as a REQUIRED ctor arg and every PRODUCTION
# call site was missing — only tests constructed it (always with a constant
# lambda). This factory closes that gap: it is the FIRST production constructor of
# `CliDispatchTools`, wiring the shared recommend-backed tier policy.
#
# Two seams source their inputs per-task (the policy itself is unchanged):
#
#   * model_for — a host-path task dict carries a PER-TASK `phase` AND
#     `assigned_persona` (the planner field; see scripts/roster.py). We REUSE
#     `_default_model_for` verbatim (no re-implementation of tier / precedence /
#     ATELIER_MODEL_TIER): for each task we resolve that task's phase and call
#     `_default_model_for(task_phase, env)` on a view of the task whose
#     `assigned_to` is the planner persona. The tier flow is the shared policy
#     (override > env > difficulty > phase > default, then ROLE_FLOOR raise-only) —
#     just sourced per-task.
#   * briefing_for — the host path is DB-free at dispatch, so `briefing_for`
#     resolves the persona from the IN-MEMORY roster
#     (scripts/roster.resolve_persona) and feeds it to
#     `compose_briefing(persona_profile_text=...)`.


def _host_model_for(
    env: Mapping[str, str] | None,
    posture: str | None = None,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build the host-path ``model_for(task, attempt) -> tier`` seam.

    REUSES ``atelier_entrypoint._default_model_for`` (the bridge's
    recommend-backed closure) so the tier precedence — explicit override > env
    ``ATELIER_MODEL_TIER`` > (reserved) difficulty > PHASE_TIER > DEFAULT_TIER,
    then the R-MODE posture transform, then ROLE_FLOOR raise-only — is byte-for-byte
    the SAME policy as the bridge.

    The only difference is the INPUT SOURCING: a host-path task carries its OWN
    ``phase`` and the planner's ``assigned_persona`` (vs the bridge's single cycle
    ``phase`` + the ``assigned_to`` DB column). For each task we read that task's
    phase, build the bridge closure for THAT phase, and invoke it on a task view
    whose ``assigned_to`` is the planner persona (so the role-floor + role signal
    flow exactly as ``_default_model_for`` expects). ``recommend`` never raises, so
    this never raises; a task with no phase / persona simply omits that signal.

    ``posture`` (M6b-2 R-MODE) — the resolved RunMode's per-task posture
    (cost-lean / neutral / opus-lean). It is threaded into ``_default_model_for``
    (→ ``recommend``) so the run-wide cost/quality bias fans out per task. The
    ``ATELIER_MODEL_TIER`` env pin retains precedence ABOVE the posture (it returns
    outright in ``recommend``), and the ROLE_FLOOR stays a HARD floor in every
    posture. ``None`` (== neutral) is byte-identical to the pre-M6b-2 output, so the
    default host wiring (balanced / no run mode) is a no-op.
    """
    # Lazy import to keep the heavy entrypoint chain off the bare import path
    # (and to avoid any import cycle: atelier_entrypoint imports dispatch which
    # cli_dispatch already imports — importing it at module top could cycle).
    from scripts.atelier_entrypoint import _default_model_for

    resolved_env: Mapping[str, str] = env if env is not None else os.environ

    def model_for(task: Mapping[str, Any], attempt: int) -> str:
        phase = task.get("phase")
        phase = phase if isinstance(phase, str) else None
        # The planner field is `assigned_persona` (== roster agent_id). Map it onto
        # the `assigned_to` key `_default_model_for` reads, falling back to an
        # already-`assigned_to`-shaped task (parity with a DB row) so this seam is
        # robust to either shape. Build a shallow view rather than mutating the
        # caller's task dict.
        role = task.get("assigned_persona")
        if role is None:
            role = task.get("assigned_to")
        task_view = {**task, "assigned_to": role}
        # Reuse the bridge closure for THIS task's phase — identical policy, plus
        # the run-wide posture (None == neutral == no-op).
        picker = _default_model_for(phase, resolved_env, posture)
        return picker(task_view, attempt)

    return model_for


def _host_briefing_for(
    *,
    clone_dir: str | os.PathLike[str],
    team_id: str,
    team_lead_name: str,
    wave_id: str,
    phase_procedure_for: Callable[[Mapping[str, Any]], str] | None = None,
    deadline_iso: str = "2099-01-01T00:00:00Z",
    include_terse: bool = True,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build the host-path ``briefing_for(task, attempt) -> str`` seam.

    Resolves the worker's persona-profile body from the IN-MEMORY roster
    (``scripts.roster.resolve_persona`` — NO DB read) and feeds it to
    ``scripts.dispatch.compose_briefing(persona_profile_text=...)``. The transport
    is pinned to ``TRANSPORT_CLI`` so the composer appends the CLI CHANNELS /
    REPLY-CONTRACT addendum (the worker returns its structured envelope, not a
    bridge send) — the host path has no bridge.

    ``phase_procedure_for(task) -> str`` supplies the dev-arc phase-procedure body
    (the bridge sources it from ``internal/<phase>/SKILL.md``); defaults to a
    deterministic stub so the factory is constructible from a minimal call site.
    An unknown persona raises ``PersonaNotInRosterError`` (fail-loud) — that runs
    inside ``pipeline``'s ``_run_one`` try-block, so it surfaces as a per-task
    failed attempt, never a hung admission loop.
    """
    from scripts.dispatch import compose_briefing
    from scripts.roster import resolve_persona

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        persona = task.get("assigned_persona")
        if persona is None:
            persona = task.get("assigned_to")
        persona_text = resolve_persona(persona)
        task_id = task.get("task_id", "")
        role_id = persona if isinstance(persona, str) else str(persona)
        if phase_procedure_for is not None:
            phase_text = phase_procedure_for(task)
        else:
            phase_text = f"(phase procedure for {task.get('phase')!r})"
        task_brief = str(task.get("description") or task.get("task_brief") or f"task {task_id}")
        return compose_briefing(
            role_id=role_id,
            task_id=task_id,
            persona_profile_text=persona_text,
            phase_procedure_text=phase_text,
            task_brief=task_brief,
            team_id=team_id,
            team_lead_name=team_lead_name,
            wave_id=wave_id,
            wave_phase=str(task.get("phase") or "implement"),
            deadline_iso=deadline_iso,
            transport=TRANSPORT_CLI,
            include_terse=include_terse,
        )

    return briefing_for


def build_cli_dispatch_for_project(
    *,
    clone_dir: str | os.PathLike[str],
    budget: BudgetPool,
    journal: ResultJournal,
    team_id: str = "host-team",
    team_lead_name: str = "team-lead",
    wave_id: str = "wave-1",
    env: Mapping[str, str] | None = None,
    model_for: Callable[[Mapping[str, Any], int], str] | None = None,
    briefing_for: Callable[[Mapping[str, Any], int], str] | None = None,
    phase_procedure_for: Callable[[Mapping[str, Any]], str] | None = None,
    runner: Runner = real_cli_runner,
    upstream_hashes_for: Callable[[Mapping[str, Any], int], Sequence[str]] | None = None,
    wall_clock_s: float = WALL_CLOCK_S,
    loop: asyncio.AbstractEventLoop | None = None,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
    allowed_tools: Sequence[str] | None = DEFAULT_ALLOWED_TOOLS,
    sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
    run_mode: Any = None,
) -> CliDispatchTools:
    """Build the production ``CliDispatchTools`` for the host/CLI transport (M6a).

    The production constructor for the host path: it constructs the leaf adapter
    with the shared recommend-backed model-tier policy, plus an in-memory
    roster-backed briefing.

    .. note::
       This factory builds the ``CliDispatchTools`` LEAF (it owns an event loop;
       drive it via the ``parallel()`` façade / ``build_cli_spawn_fn`` +
       ``build_cli_poll_fn``). The M6a PRODUCTION host caller
       (:func:`scripts.host_scheduler.run_host_pipeline_for_project`) does NOT use
       this leaf object — ``pipeline()`` consumes the seam CALLABLES directly — so
       it reuses the same seam builders (:func:`_host_model_for` /
       :func:`_host_briefing_for`) WITHOUT constructing this object. As of M6a this
       factory is therefore the sibling leaf constructor for the ``parallel()``
       façade / a future leaf-owning caller, and is presently exercised by tests
       only; it is wired to the SAME seams so a future caller inherits identical
       tier + roster behavior.

    Defaults wired in (a caller / test may override each):

    * ``model_for`` — :func:`_host_model_for`, which REUSES
      ``atelier_entrypoint._default_model_for`` so phase/role/``ATELIER_MODEL_TIER``
      precedence (and the ROLE_FLOOR opus floor for review/security/architect/
      safety) is IDENTICAL to the bridge — just sourced from the host task's
      per-task ``phase`` + planner ``assigned_persona``.
    * ``briefing_for`` — :func:`_host_briefing_for`, which resolves the persona
      profile from the in-memory roster (no DB) and composes the briefing in
      ``TRANSPORT_CLI`` mode.
    * ``est_for`` — the leaf's own ``_default_est_for`` (haiku 2k / sonnet 6k /
      opus 12k); ``CliDispatchTools`` threads it into ``run_attempt`` already, and
      :func:`pipeline` is seeded with the same ``_default_est_for`` by
      :func:`scripts.host_scheduler.run_host_pipeline_for_project`. The per-tier
      budget ceiling therefore tracks the chosen tier (a wrong tier compounds —
      it sets BOTH ``--model`` AND the seeding — which is exactly why the model_for
      flow above is the shared bridge policy, not a separate one).

    * ``run_mode`` — the resolved R-MODE :class:`~scripts.run_mode.RunMode`
      (M6b-2). When no explicit ``model_for`` is given, the run mode's ``posture``
      (cost-lean / neutral / opus-lean) is threaded into the default
      :func:`_host_model_for` so the leaf/parallel-façade path biases tiers exactly
      like the ``run_host_pipeline_for_project`` barrier-free path. **POSTURE PARITY
      with the host entrypoint:** ``run_mode=None`` is AUTO-RESOLVED via
      ``resolve_run_mode(env=env)`` (→ the saved-profile default, currently
      ``cost-lean``) — the SAME None handling as
      :func:`scripts.host_scheduler.run_host_pipeline_for_project`, so an unthreaded
      None produces the SAME posture across both sibling constructors (no
      transport-shape divergence). Only the POSTURE lever is honored here — this
      factory does NOT own the BudgetPool / fleet-width levers (those are the host
      entrypoint's), so a non-neutral mode's budget/fleet narrowing is NOT applied by
      the factory; that is by design (the leaf is a per-task adapter, not the run
      scheduler). An EXPLICITLY-NEUTRAL ``balanced`` run mode is a posture no-op.

    Use as a context manager (``with build_cli_dispatch_for_project(...) as tools:``)
    when the factory owns the event loop, so the loop is closed deterministically.
    """
    # POSTURE PARITY (FIX 5): auto-resolve a None run_mode the SAME way the host
    # entrypoint does (resolve_run_mode → saved-profile default), so the same None
    # yields the same posture regardless of which sibling constructor is used. Only
    # posture is achievable here (the factory does not own budget/fleet). An
    # EXPLICIT run mode is used as-is. Lazy import (run_mode pulls recommended_settings;
    # keep the heavy chain off the bare module-import path, parity with _host_model_for).
    from scripts.run_mode import resolve_run_mode

    eff_run_mode = run_mode if run_mode is not None else resolve_run_mode(env=env)
    pick_model = model_for if model_for is not None else _host_model_for(env, eff_run_mode.posture)
    brief = (
        briefing_for
        if briefing_for is not None
        else _host_briefing_for(
            clone_dir=clone_dir,
            team_id=team_id,
            team_lead_name=team_lead_name,
            wave_id=wave_id,
            phase_procedure_for=phase_procedure_for,
        )
    )
    return CliDispatchTools(
        budget=budget,
        journal=journal,
        clone_dir=clone_dir,
        model_for=pick_model,
        briefing_for=brief,
        runner=runner,
        upstream_hashes_for=upstream_hashes_for,
        wall_clock_s=wall_clock_s,
        loop=loop,
        permission_mode=permission_mode,
        disallowed_tools=disallowed_tools,
        allowed_tools=allowed_tools,
        sandbox_wrap=sandbox_wrap,
    )


__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_DISALLOWED_TOOLS",
    "DEFAULT_PERMISSION_MODE",
    "ENVELOPE_SCHEMA",
    "ENV_ALLOWLIST",
    "FAILED_ATTEMPT",
    "MIN_BUDGET_USD",
    "TRANSIENT_SPAWN_RETRIES",
    "TRANSPORT_CLI",
    "UNSANDBOXED_OPT_OUT_ENV",
    "CliDispatchTools",
    "CloneEscapeError",
    "FakeCliRunner",
    "Runner",
    "SandboxWrap",
    "UnsandboxedBypassError",
    "UnsandboxedRealRunError",
    "build_cli_dispatch_for_project",
    "build_cli_poll_fn",
    "build_cli_spawn_fn",
    "build_subprocess_env",
    "bwrap_sandbox_wrap",  # back-compat alias for native_sandbox_wrap
    "direct_upstream_hashes",
    "identity_sandbox_wrap",
    "is_failed_attempt",
    "max_budget_usd_for",
    "native_sandbox_wrap",
    "real_cli_runner",
    "run_attempt",
    "sandbox_prereq_status",
    "sandbox_runtime_available",
]
