# scripts/bootstrap.py
"""Atelier bootstrap — idempotent first-run initialization.

Two paths, dispatched by `scripts.mode_detector.detect_mode()`:

  - **Memex mode** (`detect_mode() == "memex"`): seed Atelier's role and
    agent catalog into Memex's `~/.memex/agents.db`, provision the
    `atelier` store via `memex:core:create-store`, and write a marker
    pinning the (atelier, memex) version pair. The procedure body
    mirrors `internal/bootstrap-memex/SKILL.md`.
  - **Local mode** (`detect_mode() == "local"`): create
    `<workspace_root>/.ai/atelier.db`, apply `migrations/shared/` THEN
    `migrations/local-only/`, and seed roles + agents into the local
    `roles` / `agents` tables. No Memex contact.

Both paths are safe to call repeatedly — every step pre-checks before
writing. On Memex upgrade past the recorded marker the bootstrap re-runs;
schema migrations through `memex:core:create-store` are idempotent so
the store provisioning is a no-op the second time.

Spec references:
  - §5  (memex coupling — bootstrap flow)
  - §6.5 (role / agent seeding)
  - §11.2 (v1.1.0 schema)
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from scripts import mode_detector, seed_data

# Atelier Tier 2 writes require Memex v2.2.0+ (caller-built
# `librarian_output` + `librarian.validate_output`). Below this floor we
# refuse to bootstrap; the user is told to upgrade Memex or fall back to
# Local mode by uninstalling Memex.
MIN_MEMEX_VERSION = (2, 2, 0)


# ── Memex version floor ───────────────────────────────────────────────────────


def _require_memex_version(floor: tuple[int, int, int] = MIN_MEMEX_VERSION) -> str:
    """Read the installed Memex plugin's version and assert it meets the
    Atelier API floor. Returns the version string on success; raises
    `RuntimeError` on too-old.

    Reads the version from the plugin manifest at the path pinned in
    `~/.memex/config.json` (resolved via `backend_memex._memex_plugin_root`).
    NOT by lex-sorting the Claude Code plugin cache — that ordering breaks
    on `2.10.0 < 2.2.0` (Plan 1 F1 / F2 contract).
    """
    from scripts import backend_memex

    plugin_root = backend_memex._memex_plugin_root()
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    version_str = data.get("version", "0.0.0")
    parsed = mode_detector._parse_version_tuple(version_str)
    # Fall back to a permissive parse on non-standard version strings so
    # the error message names the version even if it's unparseable —
    # avoids hiding "2.x-pre" behind a generic message.
    if parsed is None:
        parsed = (0, 0, 0)
    if parsed < floor:
        floor_str = ".".join(str(x) for x in floor)
        raise RuntimeError(
            f"Atelier requires Memex v{floor_str}+ (caller-built "
            f"librarian_output). Installed: v{version_str}. Upgrade memex "
            f"via agora (`claude plugin update memex`) or fall back to "
            f"Atelier Local mode by uninstalling Memex."
        )
    return version_str


# ── Hook registration ─────────────────────────────────────────────────────────


def _register_hooks() -> None:
    """Merge Atelier's ``hooks/hooks.json`` entries into ``~/.claude/settings.json``.

    Resolves ``${CLAUDE_PLUGIN_ROOT}`` to the actual plugin root path. Each
    hook is identified by its script filename (e.g. ``context_budget.py``):

    * If an entry with that filename is **absent** — the entry is appended.
    * If it is **present with a different path** (version upgrade) — the
      command is updated to the current plugin root in-place.
    * If it is **present and already matches** — the entry is left unchanged.

    Atomic write (temp file + ``os.replace``) so readers never see a partial
    file. Silently no-ops on ANY error — hook registration must never abort a
    session's pre-flight.
    """
    try:
        plugin_root = Path(__file__).resolve().parents[1]
        hooks_source = plugin_root / "hooks" / "hooks.json"
        if not hooks_source.exists():
            return

        raw: dict = json.loads(hooks_source.read_text(encoding="utf-8"))
        to_register: dict = raw.get("hooks", {})
        if not to_register:
            return

        root_str = str(plugin_root)

        def _resolve(cmd: str) -> str:
            return cmd.replace("${CLAUDE_PLUGIN_ROOT}", root_str)

        settings_file = Path.home() / ".claude" / "settings.json"
        current: dict = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            parsed = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                current = parsed

        merged = dict(current)
        existing_hooks: dict = merged.get("hooks", {})
        if not isinstance(existing_hooks, dict):
            existing_hooks = {}
        # Work on a mutable copy of each per-event list.
        new_hooks: dict = {
            evt: list(lst) for evt, lst in existing_hooks.items() if isinstance(lst, list)
        }
        changed = False

        for event_type, entries in to_register.items():
            bucket: list = new_hooks.setdefault(event_type, [])
            for entry in entries:
                for hook_spec in entry.get("hooks", []):
                    raw_cmd = hook_spec.get("command", "")
                    if not raw_cmd:
                        continue
                    resolved_cmd = _resolve(raw_cmd)
                    # Identity key: the script filename (last path component).
                    script = resolved_cmd.rsplit("/", 1)[-1]

                    # Locate any existing bucket entry that references the
                    # same script filename — from any prior version's root.
                    found_at: int | None = None
                    for i, existing_entry in enumerate(bucket):
                        for eh in existing_entry.get("hooks", []):
                            ec = eh.get("command", "")
                            if ec.rsplit("/", 1)[-1] == script:
                                found_at = i
                                if ec != resolved_cmd:
                                    # Path changed (version upgrade) — update.
                                    new_entry = dict(existing_entry)
                                    new_entry["hooks"] = [
                                        (
                                            {**eh2, "command": resolved_cmd}
                                            if eh2.get("command", "").rsplit("/", 1)[-1] == script
                                            else eh2
                                        )
                                        for eh2 in existing_entry.get("hooks", [])
                                    ]
                                    bucket[i] = new_entry
                                    changed = True
                                break
                        if found_at is not None:
                            break

                    if found_at is None:
                        # Not present — append a fully-resolved copy.
                        new_entry = dict(entry)
                        new_entry["hooks"] = [
                            ({**hs, "command": _resolve(hs["command"])} if "command" in hs else hs)
                            for hs in entry.get("hooks", [])
                        ]
                        bucket.append(new_entry)
                        changed = True

        if not changed:
            return

        merged["hooks"] = new_hooks
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".atelier-hooks-",
            suffix=".json.tmp",
            dir=str(settings_file.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, settings_file)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    except Exception:
        return  # never let hook registration abort a session's pre-flight


# ── Public entry ──────────────────────────────────────────────────────────────


def run_bootstrap(*, force: bool = False) -> dict:
    """Run the appropriate bootstrap procedure for the current mode.

    Returns a dict describing the outcome:
      - `mode` — "memex" or "local"
      - `marker` — path to the bootstrap marker file
      - `version` — atelier version string
      - mode-specific extras (`memex_version`, `db`)

    Safe to call repeatedly — each step pre-checks before writing.

    Special case (spec §5 step 0 + SKILL.md preconditions): if Memex
    appears installed (config.json + plugin) but is NOT initialized
    (registry.json absent), we refuse to silently degrade to local mode
    — instead we raise a clean RuntimeError pointing the user at
    `memex:run` so they can bootstrap Memex first. This avoids the
    pathological "half-installed Memex, atelier writes to local DB by
    surprise" footgun.

    Memex-version floor (spec §6 prerequisite): if Memex is pinned
    (`~/.memex/config.json` resolves to a memex plugin) but the manifest
    version is below `MIN_MEMEX_VERSION`, we raise BEFORE consulting
    `detect_mode`. `detect_mode` silently downgrades old memex to
    "local"; that's the right behavior for runtime code paths but it
    would let bootstrap silently write to the local DB on an old-memex
    machine — exactly the surprise we want to avoid. Reading the version
    directly here keeps bootstrap's "memex pinned but unusable" branch
    explicit.

    `force=True` bypasses the marker version-skip optimization (spec §5
    step 1). Useful for migrations and tests that need to re-seed on
    every invocation regardless of the recorded marker.
    """
    _refuse_half_installed_memex()
    _enforce_memex_version_floor()
    # Register hooks every call — idempotent and cheap. Called before the
    # marker check so existing installs pick up the hooks on the very next
    # session without waiting for a version bump to invalidate the marker.
    _register_hooks()
    if not force and _check_marker_and_skip():
        return _load_marker_result()
    mode = mode_detector.detect_mode()
    if mode == "memex":
        return _run_bootstrap_memex()
    return _run_bootstrap_local()


def _enforce_memex_version_floor() -> None:
    """If Memex appears pinned (config.json → memex plugin manifest),
    require version >= MIN_MEMEX_VERSION before letting `detect_mode`
    decide the mode.

    `detect_mode` returns "local" on under-floor Memex (intentional
    runtime fallback). Bootstrap must NOT silently fall through to local
    mode in that case — the user has Memex installed and pinned, and a
    quiet local-mode bootstrap would surprise them.

    No-op when Memex isn't pinned at all (no config.json, stale pin,
    non-memex plugin manifest) — those cases legitimately want local mode.
    """
    home = Path.home() / ".memex"
    config = home / "config.json"
    if not config.exists():
        return
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    plugin_root_str = data.get("plugin_root")
    if not plugin_root_str:
        return
    plugin_root = Path(plugin_root_str)
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        return
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if manifest_data.get("name") != "memex":
        return
    # Memex is pinned. Enforce the floor (raises on under-floor).
    _require_memex_version()


def _check_marker_and_skip() -> bool:
    """Return True if a marker exists at the mode-appropriate path AND
    its recorded `version` matches the running Atelier version.

    Reads the Memex-mode marker (`~/.memex/atelier.bootstrap.json`) when
    Memex is pinned + bootstrapped; otherwise reads the local-mode marker
    (`<workspace>/.ai/atelier.bootstrap.json`). On match, returns True so
    `run_bootstrap` can short-circuit before doing any DB / seed work.

    Returns False on:
      - missing marker
      - unreadable marker (JSON error / OSError)
      - version mismatch
      - workspace lookup failure (no git root → no local marker path)
    """
    marker_path = _marker_path_for_current_mode()
    if marker_path is None or not marker_path.exists():
        return False
    try:
        prior = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return prior.get("version") == _atelier_version()


def _marker_path_for_current_mode() -> Path | None:
    """Return the path to where the bootstrap marker would live for the
    current mode (memex vs local), or None if neither path is resolvable.

    Memex marker lives at `~/.memex/atelier.bootstrap.json` (when Memex
    is the detected mode); local marker lives at
    `<workspace_root>/.ai/atelier.bootstrap.json`. We pick based on
    `mode_detector.detect_mode()` so the marker we read agrees with the
    mode bootstrap would actually run.
    """
    mode = mode_detector.detect_mode()
    if mode == "memex":
        return Path.home() / ".memex" / "atelier.bootstrap.json"
    # Local mode — resolve the workspace root via backend_local.
    try:
        from scripts import backend_local

        return backend_local._workspace_root() / ".ai" / "atelier.bootstrap.json"
    except Exception:
        return None


def _load_marker_result() -> dict:
    """Reconstruct the result dict that the real bootstrap path would
    have returned, by reading the existing marker.

    `run_bootstrap`'s contract is to return a dict describing the run
    outcome. When we skip via the marker, we still owe the caller that
    shape — built from the marker contents plus a marker-path string.
    """
    marker_path = _marker_path_for_current_mode()
    if marker_path is None or not marker_path.exists():
        # Shouldn't reach here — _check_marker_and_skip just said True.
        return {"mode": mode_detector.detect_mode(), "version": _atelier_version(), "marker": ""}
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    result = {
        "mode": payload.get("mode", mode_detector.detect_mode()),
        "version": payload.get("version", _atelier_version()),
        "marker": str(marker_path),
    }
    if "memex_version" in payload:
        result["memex_version"] = payload["memex_version"]
    return result


def _refuse_half_installed_memex() -> None:
    """Raise a clean RuntimeError if Memex is installed but not bootstrapped.

    "Installed" = `~/.memex/config.json` exists and pins a valid plugin
    root with `name == "memex"`. "Not bootstrapped" = the registry that
    `memex_home() / "registry.json"` would point to is missing.

    The "no memex at all" case (no config.json, or pin is invalid) goes
    through cleanly to local mode without this check firing — that's the
    documented Atelier fallback.
    """
    home = Path.home() / ".memex"
    config = home / "config.json"
    if not config.exists():
        return  # no memex at all → local mode is fine
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return  # unreadable config; treat as "no memex"
    plugin_root_str = data.get("plugin_root")
    if not plugin_root_str:
        return
    plugin_root = Path(plugin_root_str)
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        return  # stale pin; treat as "no memex"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if manifest_data.get("name") != "memex":
        return
    # Memex is installed. Is it bootstrapped?
    if (home / "registry.json").exists():
        return  # bootstrapped — proceed to mode dispatch
    raise RuntimeError(
        "Memex is not bootstrapped. Run `memex:run` once to trigger "
        "Step 0.2 auto-bootstrap (or run `python3 -m scripts.install` "
        "inside the Memex plugin), then re-run Atelier bootstrap."
    )


# ── Memex-mode body ───────────────────────────────────────────────────────────


@contextlib.contextmanager
def _memex_scripts_context(plugin_root: Path):
    """Temporarily install Memex's `scripts/` as the top-level `scripts`
    package so its internal `from scripts import registry` cross-imports
    resolve to Memex modules — not to Atelier's.

    The Atelier↔Memex collision is structural: both plugins name their
    Python package `scripts`. `backend_memex._load_memex_module` works
    around it by giving each loaded module a synthetic name (e.g.
    `_memex_stores`), but that does NOT rewrite the source-level
    `from scripts import registry` statement inside `stores.py`. At
    exec time Python still resolves `scripts` via `sys.modules` and
    `sys.path`, lands on Atelier's package, and raises ImportError
    because Atelier has no `registry` submodule.

    This context manager swaps `sys.modules["scripts"]` to a fresh
    Memex `scripts` package (loaded as a namespace from `plugin_root /
    "scripts"`) for the duration of the `with` block, then restores
    Atelier's package on exit. Any modules loaded inside the block see
    the Memex package; modules loaded outside still see Atelier's.

    Bootstrap is the only Atelier code path that needs this shim — the
    documented production flow (Plan 4 sets up Memex via Memex's own
    install.py) never needs Atelier to import Memex's CRUD modules. The
    shim exists for bootstrap's narrow window of "I just provisioned a
    fresh Memex install and want to seed atelier stuff into it before
    anyone else touches it."

    On exit, drops every `scripts.*` cache entry that landed under the
    shimmed package so a later Atelier `from scripts import X` re-imports
    against Atelier's real package rather than the still-cached Memex
    siblings. (We do NOT drop `_memex_*` modules — those are loaded by
    `backend_memex._load_memex_module` via direct file-path import and
    don't participate in the `scripts.*` swap. Bootstrap also doesn't
    use `_load_memex_module` — it uses `import_module("scripts.X")`
    after the swap — so there are no `_memex_*` entries to clean up
    from this code path.)
    """
    # Save state — snapshot must happen BEFORE the try so the restore
    # in finally always has something coherent to roll back to.
    saved_scripts = sys.modules.get("scripts")
    saved_submods = {k: v for k, v in sys.modules.items() if k.startswith("scripts.")}
    saved_syspath = list(sys.path)
    try:
        # Build a fresh `scripts` package pointing at memex's scripts
        # dir. Any failure during spec-build / module-install / sys.path
        # mutation past this point lands in the finally clause and
        # restores the saved state.
        memex_scripts_init = plugin_root / "scripts" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "scripts",
            memex_scripts_init,
            submodule_search_locations=[str(plugin_root / "scripts")],
        )
        if spec is None or spec.loader is None:
            raise ImportError(
                f"failed to build import spec for memex's scripts package at {memex_scripts_init}"
            )
        memex_scripts_pkg = importlib.util.module_from_spec(spec)
        # Inject before exec_module so internal relative imports resolve
        # to the half-loaded package (Python's documented loader contract).
        sys.modules["scripts"] = memex_scripts_pkg
        # Drop Atelier's already-loaded scripts.* submodules so memex's
        # `from scripts import X` re-imports against the new package.
        for k in list(sys.modules):
            if k.startswith("scripts."):
                del sys.modules[k]
        # Put plugin_root at the head of sys.path so any `from scripts...`
        # import in the loaded modules also resolves to memex.
        sys.path.insert(0, str(plugin_root))
        spec.loader.exec_module(memex_scripts_pkg)
        yield memex_scripts_pkg
    finally:
        # Restore Atelier's package + drop every memex scripts.* module
        # so a later atelier `from scripts import X` doesn't see them.
        for k in list(sys.modules):
            if k.startswith("scripts.") or k == "scripts":
                del sys.modules[k]
        if saved_scripts is not None:
            sys.modules["scripts"] = saved_scripts
        for k, v in saved_submods.items():
            sys.modules[k] = v
        sys.path[:] = saved_syspath


def _run_bootstrap_memex() -> dict:
    """Idempotent seeding into Memex's `~/.memex/agents.db` + `atelier.db`
    via `memex:core:create-store`. Body matches `internal/bootstrap-memex/SKILL.md`.

    Hard preconditions:
      1. Memex v2.2.0+ installed (asserted by `_require_memex_version`).
      2. Memex itself is bootstrapped — `~/.memex/registry.json` exists.
         Caught + reformatted into a clean RuntimeError with operator
         guidance (NEVER let the raw `MemexNotInitializedError` reach
         the user).
    """
    from scripts import backend_memex

    plugin_root = backend_memex._memex_plugin_root()
    memex_version = _require_memex_version()

    # All Memex interactions happen inside the scripts-shim context so
    # Memex's `from scripts import registry` cross-imports resolve.
    with _memex_scripts_context(plugin_root):
        memex_db = importlib.import_module("scripts.db")
        memex_roles = importlib.import_module("scripts.roles")
        memex_agents = importlib.import_module("scripts.agents")
        memex_stores = importlib.import_module("scripts.stores")
        memex_registry = importlib.import_module("scripts.registry")

        # Precondition: memex itself must be bootstrapped. Reformat
        # MemexNotInitializedError into operator-facing guidance — DO NOT
        # let the raw exception propagate (spec §5 step 0; SKILL.md
        # preconditions).
        try:
            memex_db.require_bootstrap()
        except memex_db.MemexNotInitializedError as exc:
            raise RuntimeError(
                "Memex is not bootstrapped. Run `memex:run` once to trigger "
                "Step 0.2 auto-bootstrap (or run `python3 -m scripts.install` "
                "inside the Memex plugin), then re-run Atelier bootstrap."
            ) from exc

        memex_home = memex_db.memex_home()
        agents_db = str(memex_home / "agents.db")

        # Seed roles. Pre-check by name; on race, swallow IntegrityError
        # after confirming the row IS now present (never blind).
        role_map: dict[str, int] = _seed_roles_memex(memex_roles, agents_db)

        # Seed agents. `seed_data.load_agent_seed()` iterates the full
        # templates/agents/ directory (~61 personas), one record per .json.
        _seed_agents_memex(memex_agents, agents_db, role_map)

        # Restore memex's internal-agent invariant. Atelier has just
        # written to ~/.memex/agents.db (memex's private DB) — by contract
        # the post-touch caller must re-verify memex's own 5 internal
        # agents (librarian-1, reference-librarian-1, archivist-1, dba-1,
        # data-steward-1) are present. Memex exposes this as a public
        # hook from v2.6.0+. Soft-import for backward-compat: on older
        # memex versions the API isn't there yet — atelier continues
        # without crashing, preserving prior behavior. (Refs:
        # nitekeeper/atelier#9, nitekeeper/memex#20.)
        _ensure_memex_internal_agents(agents_db)

        # Provision the atelier store via the public registry + stores
        # API. `registry.json` is a flat `{name: record}` map per
        # memex/scripts/registry.py.
        atelier_plugin_root = Path(__file__).resolve().parents[1]
        if memex_registry.get_store("atelier") is None:
            atelier_db_path = str(memex_home / "atelier.db")
            memex_stores.create_store(
                name="atelier",
                path=atelier_db_path,
                migrations_dir=str(atelier_plugin_root / "migrations" / "shared"),
                # schema_version defaults to "v1"; create_store applies
                # migrations idempotently through its own `migrations`
                # table.
            )

        # Apply any shared migrations added AFTER the store was first provisioned
        # (create_store only runs on first provision). memex_stores.migrate is
        # idempotent — it skips files already recorded in the store's `migrations`
        # table — so this is a no-op on an up-to-date store and applies deltas
        # (e.g. the 013 dispatch-queue-drop migration) to an existing one.
        memex_stores.migrate(
            "atelier",
            str(atelier_plugin_root / "migrations" / "shared"),
        )

        # Write the marker LAST — any earlier failure leaves the marker
        # absent so the next invocation retries from the failing step.
        marker = _write_marker(memex_home, memex_version=memex_version)

    return {
        "mode": "memex",
        "memex_version": memex_version,
        "marker": str(marker),
        "version": _atelier_version(),
    }


def _seed_roles_memex(memex_roles, agents_db: str) -> dict[str, int]:
    """Pre-check + create roles into Memex's `~/.memex/agents.db`.

    Returns `{role_name: role_id}` for downstream agent seeding."""
    role_map: dict[str, int] = {}
    # One list() up front, then per-role probe against the in-memory copy.
    # On a populated DB this dodges N+1 list_roles calls per seed entry.
    existing = {r["name"]: r["id"] for r in memex_roles.list_roles(agents_db)}
    for r in seed_data.load_role_seed():
        if r["name"] in existing:
            role_map[r["name"]] = existing[r["name"]]
            continue
        try:
            new = memex_roles.create_role(agents_db, name=r["name"], description=r["description"])
            role_map[r["name"]] = new["id"]
            existing[r["name"]] = new["id"]
        except sqlite3.IntegrityError:
            # Race: another writer seeded the role between list + create.
            # Re-read and continue. Never blind — verify it's truly there.
            refreshed = {x["name"]: x["id"] for x in memex_roles.list_roles(agents_db)}
            if r["name"] in refreshed:
                role_map[r["name"]] = refreshed[r["name"]]
                existing[r["name"]] = refreshed[r["name"]]
            else:
                raise
    return role_map


def _seed_agents_memex(memex_agents, agents_db: str, role_map: dict[str, int]) -> None:
    """Pre-check + create agents into Memex's `~/.memex/agents.db`.

    Skips entries whose `role_name` wasn't in the role_map — a malformed
    seed file shouldn't bring the whole bootstrap down. (Validation lives
    in `seed_data.load_agent_seed`, so misshaped rows raise BEFORE this
    function ever sees them.)
    """
    for a in seed_data.load_agent_seed():
        if a["role_name"] not in role_map:
            # Skip orphan agent (role not present in seed). The seed
            # validator already catches gross malformations; this guard
            # exists for the niche case where role and agent seeds
            # disagree.
            continue
        if memex_agents.get_agent(agents_db, a["agent_id"]) is not None:
            continue
        try:
            memex_agents.create_agent(
                agents_db,
                a["agent_id"],
                a["name"],
                role_map[a["role_name"]],
                a["profile"],
            )
        except sqlite3.IntegrityError:
            # Race: agent landed between get + create. Confirm presence
            # then swallow.
            if memex_agents.get_agent(agents_db, a["agent_id"]) is None:
                raise


def _ensure_memex_internal_agents(agents_db: str) -> None:
    """Call `memex.scripts.install.ensure_internal_agents(agents_db)` to
    restore memex's 5 internal-agent invariant after atelier seeds its
    own roster into the same `~/.memex/agents.db`.

    MUST be called from inside `_memex_scripts_context`, where
    `sys.modules["scripts"]` resolves to memex's package — that's what
    makes `from scripts.install import ensure_internal_agents` land on
    memex's module rather than atelier's.

    Soft-import: `scripts.install.ensure_internal_agents` is a v2.6.0+
    public hook (memex PR #20). On older memex versions the module or
    symbol may be absent — in that case we log a structured warning to
    stderr and return without crashing. Atelier's own bootstrap still
    completes; the user can recover by running `python3 -m scripts.install`
    from the memex plugin root after upgrading.

    Any other failure inside `ensure_internal_agents` (e.g.
    `InternalAgentsMissingError` from a corrupted DB) is also surfaced
    to stderr but does NOT abort atelier bootstrap — atelier's own
    seeding has already landed by this point and the marker write that
    follows is the only side effect we still owe the caller. Aborting
    here would only re-trigger this same code path on the next run.
    """
    try:
        from scripts.install import ensure_internal_agents  # type: ignore[attr-defined]
    except ImportError:
        # memex < 2.6.0 — no public hook. Pre-#9 behavior preserved.
        print(
            "atelier.bootstrap: memex.scripts.install.ensure_internal_agents "
            "unavailable (memex < 2.6.0); skipping internal-agent restore. "
            "If memex's 5 internal agents are missing, recover via "
            "`python3 -m scripts.install` from the memex plugin root.",
            file=sys.stderr,
        )
        return

    try:
        ensure_internal_agents(agents_db)
    except Exception as exc:
        # InternalAgentsMissingError or anything else. We log and
        # continue so the marker write still happens; aborting here
        # would just retry the same failing call on the next invocation.
        print(
            f"atelier.bootstrap: ensure_internal_agents({agents_db}) failed: "
            f"{type(exc).__name__}: {exc}. Bootstrap will continue; recover "
            f"via `python3 -m scripts.install` from the memex plugin root.",
            file=sys.stderr,
        )


# ── Local-mode body ───────────────────────────────────────────────────────────


def _run_bootstrap_local() -> dict:
    """Idempotent provisioning of `<workspace_root>/.ai/atelier.db`.

    Steps:
      1. Resolve workspace_root (git root containing CWD).
      2. Apply `migrations/shared/` THEN `migrations/local-only/` against
         `.ai/atelier.db` — both runners are idempotent (skip applied
         filenames).
      3. Seed roles + agents into the local `roles` / `agents` tables via
         `backend_local.find_or_create_role/agent`.
      4. Write the marker.

    No Memex contact whatsoever — Local mode is the slim fallback. Skipping
    the version check is intentional (spec §7).
    """
    from scripts import backend_local

    # Reuse backend_local's workspace_root resolver — it's the canonical
    # CWD → git_root resolver and avoids importing scripts.workspace
    # (which has tmux side-effects).
    workspace_root = backend_local._workspace_root()
    ai_dir = workspace_root / ".ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    db_path = ai_dir / "atelier.db"

    # Apply migrations. The migrate runner skips files already in the
    # `migrations` table, so re-running on a populated DB is a no-op.
    from scripts.migrate import apply_migrations

    migrations_root = Path(__file__).resolve().parents[1] / "migrations"
    apply_migrations(str(db_path), migrations_root / "shared")
    apply_migrations(str(db_path), migrations_root / "local-only")

    # Seed roles + agents. backend_local.find_or_create_role/agent
    # pre-checks before insert, so this is naturally idempotent.
    role_map: dict[str, int] = {}
    for r in seed_data.load_role_seed():
        row = backend_local.find_or_create_role(name=r["name"], description=r["description"])
        role_map[r["name"]] = row["id"]

    for a in seed_data.load_agent_seed():
        if a["role_name"] not in role_map:
            continue
        backend_local.find_or_create_agent(
            agent_id=a["agent_id"],
            name=a["name"],
            role_id=role_map[a["role_name"]],
            profile=a["profile"],
        )

    marker = _write_marker(ai_dir, mode="local")
    return {
        "mode": "local",
        "db": str(db_path),
        "marker": str(marker),
        "version": _atelier_version(),
    }


# ── Marker + version helpers ──────────────────────────────────────────────────


def _atelier_version() -> str:
    """Resolve the atelier package version; fall back to a dev sentinel
    when the package isn't pip-installed (the common case in worktrees)."""
    try:
        import importlib.metadata as md

        return md.version("atelier")
    except Exception:
        return "1.14.0"


def _write_marker(
    marker_root: Path, *, memex_version: str | None = None, mode: str = "memex"
) -> Path:
    """Write the bootstrap marker to `<marker_root>/atelier.bootstrap.json`.

    The marker is consulted by every Atelier command on startup — when it
    matches the current atelier version, bootstrap is skipped in O(1).

    For Memex mode the marker lives at `~/.memex/atelier.bootstrap.json`.
    For Local mode it lives at `<workspace>/.ai/atelier.bootstrap.json`.
    """
    marker = marker_root / "atelier.bootstrap.json"
    payload: dict = {
        "mode": mode,
        "version": _atelier_version(),
        "bootstrapped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if memex_version is not None:
        payload["memex_version"] = memex_version
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return marker


if __name__ == "__main__":  # pragma: no cover
    result = run_bootstrap()
    print(json.dumps(result, indent=2))
    sys.exit(0)
