# scripts/loom_comms.py
"""Atelier-side wrapper around the loom-agent-chat client (gated, bridge-fallback).

This module is the atelier-side adapter for the **loom-agent-chat** plugin's
``loom_chat.py`` CLI — it is NOT the loom client itself. It carries the
team-mode PEER-to-PEER chat, the plan-phase KICKOFF meeting, and the PM's
team/individual GOALS over Loom **when Loom is available**, and degrades to a
byte-identical bridge-only path when it is not.

## The architecture boundary (read this first)

Loom carries the **conversational** traffic:

* peer-to-peer (PEER) chat between teammates,
* the plan-phase kickoff MEETING,
* the PM's TEAM goal + per-agent INDIVIDUAL goals.

Loom **never** carries control-flow envelopes. The terminal ``task_result``
reply envelope (TM-006) ALWAYS rides the existing BRIDGE
(``scripts/bridge_send.py`` → ``bridge_messages``) — that is the control-plane
the ``WaveDispatcher`` ``poll_fn`` reads. A worker's reply envelope, heartbeats,
and every other control signal stay on the bridge regardless of whether Loom is
up. This separation is the whole point: Loom is a conversational overlay
(mandatory when available) on top of the mandatory bridge control-plane —
the bridge stays the sole control-plane, and Loom is never a replacement for
it.

## The availability gate (fail-soft, NEVER raise)

Every entry point is GATED on Loom availability via :func:`detect`. If Loom is
unavailable — the client binary is missing, ``loom_chat.py detect`` exits
non-zero, the server is down, or ANY exception is raised — behavior is
byte-identical to today (bridge-only). No cycle may EVER crash because Loom is
down. The orchestration helpers (:func:`kickoff` / :func:`invite` /
:func:`deregister`) are each individually fail-soft: a transport error degrades
to "not posted", it never propagates.

## The loom_chat.py CLI contract this wraps (do not invent shape)

``loom_chat.py`` (the bundled stdlib-only client) exposes:

* ``detect`` → prints ``{"available": true, "url": ..., "port": ..., "source": ...}``
  on exit 0, or ``{"available": false}`` on exit 3.
* ``register <name>`` → prints ``{"assigned_name": ..., "session_id": ..., "url": ...}``.
* ``create-channel <name> --as <name>`` → prints the server's channel JSON.
* ``join <channel> --as <name>`` → prints the server's join JSON.
* ``send <channel> <to> <body...> --as <name>`` → prints the server's send JSON;
  ``to`` is a teammate member name for a directed send, or ``@here`` for the
  deliberate kickoff broadcast. Bodies over the cap (default 500) are REJECTED
  by the client with exit 2 — callers MUST doc-spill long content.
* ``deregister --as <name>`` → marks the agent gone; chat history is retained.

All commands are list-form ``[python3, <client>, ...]`` — never ``shell=True``.
``role_id`` / ``channel`` / goal text are DATA, never interpolated into a shell.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 — we invoke loom_chat.py via list-form argv only; never shell=True.
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

#: Highest-precedence override: an explicit path to the loom client. Mirrors the
#: loom client's own ``LOOM_*`` env convention but is atelier-scoped (we are
#: resolving WHICH client file to run, not where its server lives).
LOOM_CLIENT_ENV_VAR = "LOOM_CLIENT"

#: The SINGLE opt-out env var (atelier analog of kaizen's ``KAIZEN_LOOM_COMMS``).
#: ``"0"`` is the ONLY value that disables Loom; unset — or any other value —
#: leaves Loom mandatory-when-available. Gated inside :func:`detect` (the single
#: availability choke point every helper reads ``status`` from) so one check
#: covers team mode, subagent mode, and every orchestration helper. When set to
#: ``"0"`` the cycle degrades byte-identical to bridge-only — no behaviour
#: change, only the obligation is lifted.
LOOM_COMMS_ENV_VAR = "ATELIER_LOOM_COMMS"

#: The agora plugin-cache root where loom-agent-chat is installed. We mirror the
#: version-sort discovery atelier uses for its own plugin lookups: the client
#: lives at ``<cache>/loom-agent-chat/<ver>/skills/loom-chat/loom_chat.py``.
_AGORA_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache" / "agora"
_LOOM_PLUGIN_DIR = "loom-agent-chat"
_LOOM_CLIENT_RELPATH = Path("skills") / "loom-chat" / "loom_chat.py"

#: Default per-message body cap (chars). The loom client enforces this itself
#: (``send`` exits 2 past the cap); we mirror the value so the atelier-side
#: helpers can doc-spill BEFORE issuing an over-cap send rather than eating an
#: exit-2. Kept in sync with ``loom_chat.DEFAULT_MAX_BODY``.
DEFAULT_MAX_BODY = 500

#: The default channel-relative temp dir Loom uses for doc-spilled long content.
#: A goal/message longer than ``max_body`` is written here and a short pointer is
#: posted instead (req 6).
DEFAULT_TEMP_DIR = ".loom/temp"

#: Inclusive upper bound of :func:`teardown`'s collision-suffix sweep. The Loom
#: server auto-suffixes a colliding registration (``scout`` → ``scout-2`` — see
#: the loom client's own contract), so a worker that re-registered after a stale
#: session can linger under ``<name>-2``/``<name>-3``/... after a verbatim-name
#: sweep. The loom_chat CLI exposes NO member-listing command (``list-channels``
#: lists channels, not registrants), so :func:`teardown` sweeps the
#: DETERMINISTIC variants ``<name>-2`` .. ``<name>-<this bound>`` instead.
#: Bounded small on purpose: each cycle re-registers a given role at most a
#: handful of times, and every extra variant costs one (fail-soft, no-op-safe)
#: subprocess per swept name.
TEARDOWN_COLLISION_SWEEP_MAX = 4


# ── Client resolution ──────────────────────────────────────────────────────


def _version_key(name: str) -> tuple[int, ...]:
    """Robust numeric version sort key for an ``<X.Y.Z>`` cache dir name.

    Splits on ``.`` and int-compares each component so ``2.10`` sorts ABOVE
    ``2.2`` (a lexical sort would invert them). Non-numeric components collapse
    to ``-1`` so a malformed dir name sorts BELOW any well-formed version rather
    than crashing the discovery. Today only ``0.1.0`` exists, but the key is
    written correctly so a future bump is not a silent mis-sort.
    """
    parts: list[int] = []
    for component in name.split("."):
        try:
            parts.append(int(component))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def resolve_loom_client(env: Mapping[str, str] | None = None) -> Path | None:
    """Resolve the path to the loom client (``loom_chat.py``), or ``None``.

    Precedence (highest first):

    1. ``env[LOOM_CLIENT]`` — an explicit operator override. Honored ONLY if the
       path actually exists (a dangling override is treated as "not found" so a
       stale env var never wedges discovery).
    2. The agora plugin cache: the HIGHEST-version dir under
       ``~/.claude/plugins/cache/agora/loom-agent-chat/<ver>/`` whose
       ``skills/loom-chat/loom_chat.py`` actually exists. We sort candidate
       version dirs with :func:`_version_key` (numeric, not lexical) and pick the
       newest whose client file is present — validating the file rather than
       trusting the dir name.
    3. ``None`` — loom-agent-chat is not installed. Callers gate on this:
       ``None`` ⇒ Loom unavailable ⇒ bridge-only fallback.

    ``env`` defaults to ``os.environ`` (read lazily so tests can inject a dict).
    Never raises — discovery failure is a ``None`` return, mirroring the
    fail-soft posture of the whole module.
    """
    if env is None:
        import os

        env = os.environ

    override = env.get(LOOM_CLIENT_ENV_VAR)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        # A set-but-dangling override is "not found", not a hard error — fall
        # through to cache discovery so a stale env var never wedges Loom.

    plugin_root = _AGORA_CACHE_ROOT / _LOOM_PLUGIN_DIR
    try:
        version_dirs = [d for d in plugin_root.iterdir() if d.is_dir()]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None

    # Newest version first; pick the first whose client file actually exists.
    for version_dir in sorted(version_dirs, key=lambda d: _version_key(d.name), reverse=True):
        client = version_dir / _LOOM_CLIENT_RELPATH
        if client.is_file():
            return client
    return None


# ── Detection gate ─────────────────────────────────────────────────────────


@dataclass
class LoomStatus:
    """The resolved Loom availability snapshot — the gate's verdict.

    ``available`` is the ONLY field callers branch on; the rest are diagnostic /
    routing context for the orchestration helpers (channel URL, port, discovery
    source, and the resolved client path so helpers re-run the same binary).
    """

    available: bool
    url: str | None = None
    port: int | None = None
    source: str | None = None
    client: Path | None = None


def detect(
    *,
    client: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> LoomStatus:
    """Probe Loom availability — the availability GATE (req 1). Never raises.

    Resolves the client via :func:`resolve_loom_client` if not supplied; a
    ``None`` client (loom-agent-chat not installed) short-circuits to
    ``LoomStatus(available=False)``. Otherwise runs
    ``[sys.executable, <client>, "detect"]`` through the injected ``runner`` and
    parses its stdout JSON.

    Availability is TRUE iff: the runner returns exit 0 AND the parsed stdout
    JSON has ``available is True``. EVERY other outcome — a non-zero exit, an
    unparseable / empty stdout, a missing ``available`` key, or ANY exception
    raised by the runner — collapses to ``available=False`` (fail-soft
    fallback). This is the byte-identical-to-bridge guarantee: a down Loom is
    indistinguishable from an absent one, and neither can crash a cycle.
    """
    if env is None:
        import os

        env = os.environ
    # Single opt-out gate (req 1): ``ATELIER_LOOM_COMMS=0`` disables Loom before
    # any client resolution or subprocess — the runner is never invoked. This is
    # the FIRST thing detect() checks so the opt-out covers every helper that
    # routes through ``status``. "0" is the only opt-out value.
    if env.get(LOOM_COMMS_ENV_VAR) == "0":
        return LoomStatus(available=False)
    if client is None:
        client = resolve_loom_client(env=env)
    if client is None:
        return LoomStatus(available=False)

    try:
        proc = runner(
            [sys.executable, str(client), "detect"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        # ANY runner failure (OSError, the binary vanished mid-run, a fake
        # runner raising in tests) is fail-soft: Loom is unavailable.
        return LoomStatus(available=False)

    if getattr(proc, "returncode", 1) != 0:
        return LoomStatus(available=False)

    payload = _parse_json(getattr(proc, "stdout", ""))
    if not isinstance(payload, Mapping) or payload.get("available") is not True:
        return LoomStatus(available=False)

    port = payload.get("port")
    return LoomStatus(
        available=True,
        url=payload.get("url"),
        port=port if isinstance(port, int) else None,
        source=payload.get("source"),
        client=client,
    )


def _parse_json(text: object) -> object | None:
    """Best-effort parse of a JSON object from command stdout. Returns ``None``
    on any non-string / empty / unparseable input — the caller treats that as a
    fail-soft "unavailable", never an exception."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


# ── Command-string templates (briefing-rendered) ───────────────────────────


def loom_cmds(
    *,
    role_id: str,
    channel: str,
    client: str | Path,
    team_lead_name: str,
    max_body: int = DEFAULT_MAX_BODY,
) -> dict[str, str]:
    """Build the RUNNABLE Loom command strings the briefing renders.

    The analog of ``dispatch._default_bridge_cmds`` for the Loom transport: a
    dict of one-line ``python3 <client> ...`` command strings (plus the
    ``channel`` / ``max_body`` literals and a one-line ``doc_spill`` rule) that
    the ``role.j2`` template interpolates into the worker's CHANNELS block.

    These are concrete commands a worker can run VERBATIM — the resolved client
    path (``client``, the absolute ``loom_chat.py`` path from
    :attr:`LoomStatus.client`) and the lead handle (``team_lead_name``) are
    substituted in directly. The ONLY placeholders that remain are the ones a
    worker legitimately fills at send time: ``<peer_role_id>`` (the caller picks
    which peer to address), ``<body>`` (the message), and ``<id>`` (the inbox
    message id to mark read). No ``<loom_client>`` / ``<team_lead_role_id>``
    placeholder survives into the rendered briefing.

    Keys: ``register``, ``send_to_peer``, ``send_to_lead``, ``read_inbox``,
    ``mark_read``, ``deregister``, ``channel``, ``max_body``, ``doc_spill``.
    """
    return {
        "register": f"python3 {client} register {role_id}",
        "send_to_peer": (f'python3 {client} send {channel} <peer_role_id> "<body>" --as {role_id}'),
        "send_to_lead": (
            f'python3 {client} send {channel} {team_lead_name} "<body>" --as {role_id}'
        ),
        "read_inbox": f"python3 {client} read {channel} --as {role_id}",
        "mark_read": f"python3 {client} mark-read <id> --as {role_id}",
        "deregister": f"python3 {client} deregister --as {role_id}",
        "channel": channel,
        "max_body": str(max_body),
        "doc_spill": (
            f"Chat bodies are capped at {max_body} chars. For longer content, "
            f"write it to .loom/temp/<name>.md and post a SHORT pointer message "
            f"with that path + a 1-2 sentence summary — never paste the full body."
        ),
    }


def build_team_chat_context(
    status: LoomStatus,
    *,
    role_id: str,
    channel: str,
    team_lead_name: str,
    max_body: int = DEFAULT_MAX_BODY,
) -> dict:
    """Build the ``team_chat`` ctx dict for ``compose_briefing``.

    ALWAYS returns a dict, NEVER ``None`` (so ``validate_render_context`` — which
    treats ``None`` as missing — passes on BOTH paths and the AST-union test
    stays consistent). The ``transport`` key is the branch the template reads:

    * ``status.available`` AND ``status.client is not None`` ⇒
      ``{"transport": "loom", "channel": ..., "cmds": loom_cmds(...),
      "max_body": ...}`` — the template renders the Loom chat protocol
      subsection with RUNNABLE commands (the resolved client path + the concrete
      lead handle are baked into ``cmds`` via ``loom_cmds``).
    * otherwise ⇒ ``{"transport": "bridge"}`` — the template renders ONLY the
      existing bridge CHANNELS block (byte-identical to pre-Loom). A status that
      reports ``available`` but carries no resolved ``client`` path cannot
      produce runnable commands, so it ALSO falls back to bridge (never emits a
      half-resolved Loom block).
    """
    if status.available and status.client is not None:
        return {
            "transport": "loom",
            "channel": channel,
            "cmds": loom_cmds(
                role_id=role_id,
                channel=channel,
                client=status.client,
                team_lead_name=team_lead_name,
                max_body=max_body,
            ),
            "max_body": max_body,
        }
    return {"transport": "bridge"}


# ── PM-side orchestration helpers (each fail-soft) ─────────────────────────


def _run_loom(
    status: LoomStatus,
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess],
) -> subprocess.CompletedProcess | None:
    """Run one ``loom_chat.py`` subcommand through the injected runner.

    Returns the ``CompletedProcess`` on a clean (exit 0) call, or ``None`` on
    ANY failure — a non-zero exit, a missing client, or an exception. This is
    the single fail-soft choke point every PM-side helper routes through, so a
    transport error NEVER propagates out of an orchestration call. ``args`` are
    list-form argv (never shell); ``status.client`` / ``role_id`` / goal text are
    DATA.
    """
    client = status.client
    if client is None:
        return None
    try:
        proc = runner(
            [sys.executable, str(client), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return proc


def _run_loom_raw(
    status: LoomStatus,
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess],
) -> subprocess.CompletedProcess | None:
    """Run one ``loom_chat.py`` subcommand and return the RAW ``CompletedProcess``.

    Unlike :func:`_run_loom` (which collapses ANY non-zero exit to ``None``),
    this returns the process REGARDLESS of exit code so a caller can inspect
    ``returncode`` / ``stderr``. Required by :func:`rejoin` to tell a stale-
    session failure (the loom client maps a server ``HTTP Error 400`` — and a
    dropped local session — to a non-zero exit; recoverable via
    re-register → re-join) apart from a clean success. Returns ``None`` ONLY when
    the client is missing or the runner itself raises — still fail-soft, never
    propagates. ``args`` are list-form argv (never shell); ``status.client`` is
    DATA.
    """
    client = status.client
    if client is None:
        return None
    try:
        return runner(
            [sys.executable, str(client), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None


def _slug(name: str) -> str:
    """Filesystem-safe slug for a doc-spill temp filename. Keeps alnum / dash /
    underscore; everything else (incl. path separators) becomes ``-`` so an
    attacker-supplied member name cannot escape ``temp_dir``. Capped at 64 chars
    so an untrusted long member name / ``individual_goals`` key cannot blow up
    the filename length."""
    safe = [c if (c.isalnum() or c in "-_") else "-" for c in name]
    slug = "".join(safe).strip("-")[:64]
    return slug or "goal"


def _post_goal(
    status: LoomStatus,
    *,
    channel: str,
    to: str,
    body: str,
    slug: str,
    pm_as: str,
    runner: Callable[..., subprocess.CompletedProcess],
    temp_dir: str,
    max_body: int,
) -> tuple[bool, str | None]:
    """Post one goal to ``to`` on ``channel`` as ``pm_as``, doc-spilling if over
    ``max_body``.

    Returns ``(posted, spilled_path)``: ``posted`` is True iff the send (pointer
    or direct) succeeded; ``spilled_path`` is the temp-file path written when the
    body exceeded ``max_body`` (else ``None``). Fail-soft — a failed send returns
    ``(False, ...)`` and never raises (req 6 doc-spill + the never-crash gate).
    """
    spilled_path: str | None = None
    if len(body) > max_body:
        path = Path(temp_dir) / f"{slug}.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError:
            # Cannot spill → cannot post a pointer to a file that does not
            # exist; fail-soft "not posted" for this one goal.
            return (False, None)
        # Post the ABSOLUTE path (matching the loom client's absolute-path
        # convention) so a peer with a different cwd can open it. Kept concise so
        # a realistic absolute path still fits under max_body.
        abs_path = str(path.resolve())
        spilled_path = abs_path
        send_body = f"[goal>{max_body}c -> {abs_path}]"
    else:
        send_body = body

    proc = _run_loom(
        status,
        ["send", channel, to, send_body, "--as", pm_as],
        runner=runner,
    )
    return (proc is not None, spilled_path)


def kickoff(
    *,
    status: LoomStatus,
    channel: str,
    team_goal: str,
    individual_goals: Mapping[str, str],
    members: list[str],
    pm_name: str = "team-lead",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    temp_dir: str = DEFAULT_TEMP_DIR,
    max_body: int = DEFAULT_MAX_BODY,
) -> dict:
    """PM kickoff: register, open the channel, post the TEAM goal + per-agent
    INDIVIDUAL goals. Gated + fail-soft.

    * If ``not status.available`` ⇒ return ``{"transport": "bridge", "posted":
      False}`` and post NOTHING — the caller falls back to the bridge meeting
      (``scripts/team_meeting.py``), which is NOT deleted (req 4 fallback).
    * Else: register ``pm_name``; create the channel; post ``team_goal`` to
      ``@here`` (the deliberate kickoff broadcast — req 3); then for EACH member
      post ``individual_goals[member]`` as a DIRECTED send to that member
      (req 3). Any goal longer than ``max_body`` is doc-spilled to
      ``temp_dir/<slug>.md`` with a short pointer posted instead (req 6).

    Returns ``{"transport": "loom", "posted": True, "messages": <count of
    successful posts>, "spilled": [<paths>]}``. Each post is independently
    fail-soft — one failed individual goal does not abort the rest, and the
    ``messages`` count reflects only what actually landed so a silent drop is
    observable to the caller.

    Role-ids are assumed COLLISION-FREE per cycle: the per-cycle Loom channel +
    a roster of distinct role-ids guarantee the Loom server never has to
    collision-rename a member (which would return an ``assigned_name`` differing
    from the requested one and silently mis-address directed sends). We make that
    assumption load-bearing + visible by asserting ``members`` is duplicate-free
    here (raising :class:`ValueError`) rather than threading ``assigned_name``
    back through every directed send.
    """
    if len(set(members)) != len(members):
        raise ValueError(
            "kickoff requires a duplicate-free members roster (role-ids are "
            "assumed collision-free per cycle so the Loom server never "
            f"collision-renames a member); got {members!r}"
        )
    if not status.available:
        return {"transport": "bridge", "posted": False}

    posted = 0
    spilled: list[str] = []

    # Register the PM + open the channel (each fail-soft; a failure here just
    # means the subsequent sends will themselves fail-soft to "not posted").
    _run_loom(status, ["register", pm_name], runner=runner)
    _run_loom(status, ["create-channel", channel, "--as", pm_name], runner=runner)

    # TEAM goal → @here (the one deliberate broadcast — the kickoff).
    team_ok, team_spill = _post_goal(
        status,
        channel=channel,
        to="@here",
        body=team_goal,
        slug="team-goal",
        pm_as=pm_name,
        runner=runner,
        temp_dir=temp_dir,
        max_body=max_body,
    )
    if team_ok:
        posted += 1
        # Only record the spill once its pointer SEND succeeded — `spilled` means
        # "spilled AND pointed-to", not merely "file written".
        if team_spill is not None:
            spilled.append(team_spill)

    # One DIRECTED individual goal per member (TM-003 addressivity).
    for member in members:
        goal = individual_goals.get(member)
        if goal is None:
            continue
        ok, spill = _post_goal(
            status,
            channel=channel,
            to=member,
            body=goal,
            slug=_slug(member),
            pm_as=pm_name,
            runner=runner,
            temp_dir=temp_dir,
            max_body=max_body,
        )
        if ok:
            posted += 1
            # Append only after the pointer SEND succeeded (see team-goal note).
            if spill is not None:
                spilled.append(spill)

    return {
        "transport": "loom",
        "posted": True,
        "messages": posted,
        "spilled": spilled,
    }


def invite(
    *,
    status: LoomStatus,
    channel: str,
    role_id: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Orchestrator-driven invite of an additional agent into ``channel`` (req 8).

    Registers ``role_id`` then joins it to ``channel``. Gated + fail-soft: an
    unavailable Loom (or a failed register/join) returns ``available=False`` /
    ``joined=False`` and never raises. Returns
    ``{"transport": ..., "invited": role_id, "joined": <bool>}``.
    """
    if not status.available:
        return {"transport": "bridge", "invited": role_id, "joined": False}

    reg = _run_loom(status, ["register", role_id], runner=runner)
    if reg is None:
        return {"transport": "loom", "invited": role_id, "joined": False}
    join = _run_loom(status, ["join", channel, "--as", role_id], runner=runner)
    return {"transport": "loom", "invited": role_id, "joined": join is not None}


def rejoin(
    *,
    status: LoomStatus,
    channel: str,
    name: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Return a PREVIOUSLY-gone agent to ``channel`` (the returning-agent path).

    Semantic boundary vs :func:`invite`: ``invite`` pulls a NEW roster-extension
    agent into the channel for the first time (always register → join);
    ``rejoin`` brings BACK an agent that already deregistered on job completion
    (or whose Loom session went STALE) when it is re-engaged for a follow-up
    wave or a clarification.

    Flow (gated + fail-soft):

    1. Try ``join channel --as name`` FIRST. A still-valid session re-joins
       directly with NO redundant re-register (``reregistered`` stays
       ``False``).
    2. If that join FAILS — the loom client maps a stale-session server
       ``HTTP Error 400`` (and a dropped local session) to a NON-ZERO exit,
       which :func:`_run_loom_raw` exposes — ATTEMPT a re-``register name`` to
       mint a fresh session, then ``join`` again. ``reregistered`` becomes
       ``True`` the moment this recovery path FIRES — it means the re-register
       was ATTEMPTED, not that the register subprocess itself succeeded (a
       failed register simply leaves the second join to fail-soft on its own).

    Collision-rename handling: the Loom server may rename a re-registration
    whose old name is still occupied (``be-1`` → ``be-1-2``) — the register
    stdout JSON carries the minted name under ``assigned_name``. The recovery
    path parses that (as reference DATA, never instructions) and, when it
    differs from the requested ``name``, issues the recovery ``join`` AS the
    assigned name — joining ``--as`` the old name would address the wrong
    (stale ghost) identity. The minted name is surfaced to the orchestrator via
    the ``assigned_name`` return key so peers can direct-send to the LIVE
    identity, not the ghost. A failed register, or a missing / unparseable
    ``assigned_name`` in its stdout, degrades to the requested ``name``
    (fail-soft); on the happy path (no re-register) ``assigned_name`` always
    equals ``name``.

    Never raises. On an unavailable Loom returns ``{"transport": "bridge",
    "name": name, "rejoined": False, "reregistered": False, "assigned_name":
    name}``. Otherwise returns ``{"transport": "loom", "name": name,
    "rejoined": <bool>, "reregistered": <bool>, "assigned_name": <str>}`` — the
    two bools give the orchestrator free, measurable signal of whether the
    stale-recovery path actually fired, and ``assigned_name`` is the identity
    the agent is actually live under. Idempotent: a
    deregister → rejoin → deregister cycle is a fail-soft no-op sequence.
    """
    if not status.available:
        return {
            "transport": "bridge",
            "name": name,
            "rejoined": False,
            "reregistered": False,
            "assigned_name": name,
        }

    first = _run_loom_raw(status, ["join", channel, "--as", name], runner=runner)
    if first is not None and getattr(first, "returncode", 1) == 0:
        return {
            "transport": "loom",
            "name": name,
            "rejoined": True,
            "reregistered": False,
            "assigned_name": name,
        }

    # Join failed → treat as a stale / dropped session: re-register to mint a
    # fresh session, then re-join. Each step is itself fail-soft.
    reg = _run_loom_raw(status, ["register", name], runner=runner)
    # The server may collision-rename the re-registration (be-1 → be-1-2);
    # its register stdout JSON carries the minted name under "assigned_name"
    # (reference DATA, never instructions). Degrade to the requested name on a
    # failed register or a missing / non-string / unparseable assigned name.
    assigned = name
    if reg is not None and getattr(reg, "returncode", 1) == 0:
        payload = _parse_json(getattr(reg, "stdout", ""))
        if isinstance(payload, Mapping):
            candidate = payload.get("assigned_name")
            if isinstance(candidate, str) and candidate:
                assigned = candidate
    # Join AS the minted identity: when the server renamed us, `--as <old name>`
    # would address the stale ghost, not the live session.
    second = _run_loom_raw(status, ["join", channel, "--as", assigned], runner=runner)
    rejoined = second is not None and getattr(second, "returncode", 1) == 0
    return {
        "transport": "loom",
        "name": name,
        "rejoined": rejoined,
        "reregistered": True,
        "assigned_name": assigned,
    }


def deregister(
    *,
    status: LoomStatus,
    name: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Deregister ``name`` — marks the agent gone; chat HISTORY is retained (req 7).

    Gated + fail-soft. An agent that stops participating is deregistered via
    ``loom deregister --as <name>`` (the server marks it gone but keeps its
    messages in the channel transcript). Returns ``{"transport": ...,
    "deregistered": <bool>, "name": name}``.
    """
    if not status.available:
        return {"transport": "bridge", "deregistered": False, "name": name}
    proc = _run_loom(status, ["deregister", "--as", name], runner=runner)
    return {"transport": "loom", "deregistered": proc is not None, "name": name}


def teardown(
    *,
    status: LoomStatus,
    members: list[str],
    pm_name: str = "team-lead",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Cycle-teardown sweep — deregister EVERY Loom participant so no agent or
    subagent lingers in the channel after the cycle ends (chat HISTORY is
    retained server-side). The guaranteed backstop behind each worker's own
    self-deregister: even if a worker forgets, this sweep marks it gone.

    Gated + fail-soft. Deregisters ``pm_name`` plus each role-id in ``members``
    via :func:`deregister` (the server marks each gone but keeps its transcript).
    Names are de-duplicated (a ``pm_name`` that also appears in ``members`` is
    swept once) while preserving order. Idempotent + order-independent: sweeping
    an already-gone or never-registered name is a fail-soft no-op, and one
    member's failed deregister never aborts the rest.

    After the verbatim sweep, COLLISION-SUFFIXED variants are swept too: a
    re-registration (e.g. :func:`rejoin`'s stale-session recovery) can be
    collision-renamed by the Loom server (``be-1`` → ``be-1-2``), and that
    minted name would otherwise linger after a verbatim-only sweep. The
    loom_chat CLI has no member-listing command to discover the actual
    registrants, so we sweep the deterministic variants ``<name>-2`` ..
    ``<name>-<TEARDOWN_COLLISION_SWEEP_MAX>`` for every swept name (see the
    constant's doc for the bound rationale). Each variant deregister is the
    same fail-soft no-op as the base sweep — a never-minted variant simply
    fails to confirm — and a variant that already appears as a base name is
    not swept twice.

    Returns ``{"transport": "loom", "deregistered": [<names confirmed gone>],
    "attempted": [<every name swept>], "variants_attempted": [<every collision
    variant swept>], "variants_deregistered": [<variants confirmed gone>]}`` —
    the original ``attempted`` / ``deregistered`` keys keep their verbatim-name
    semantics (backward-compatible); the variant sweep reports additively. On
    an unavailable Loom returns the same shape under ``"transport": "bridge"``
    with all four lists empty — nothing registered, nothing to sweep.
    """
    if not status.available:
        return {
            "transport": "bridge",
            "deregistered": [],
            "attempted": [],
            "variants_attempted": [],
            "variants_deregistered": [],
        }
    names: list[str] = []
    for name in [pm_name, *members]:
        if name not in names:
            names.append(name)
    gone = [
        name
        for name in names
        if deregister(status=status, name=name, runner=runner)["deregistered"]
    ]
    # Collision-suffix sweep: deterministic <name>-2..<name>-N variants, skipping
    # any that already appeared as a base name (no double sweep).
    seen = set(names)
    variants: list[str] = []
    for name in names:
        for suffix in range(2, TEARDOWN_COLLISION_SWEEP_MAX + 1):
            variant = f"{name}-{suffix}"
            if variant not in seen:
                seen.add(variant)
                variants.append(variant)
    variants_gone = [
        variant
        for variant in variants
        if deregister(status=status, name=variant, runner=runner)["deregistered"]
    ]
    return {
        "transport": "loom",
        "deregistered": gone,
        "attempted": names,
        "variants_attempted": variants,
        "variants_deregistered": variants_gone,
    }
