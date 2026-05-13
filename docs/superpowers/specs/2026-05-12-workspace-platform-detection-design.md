# Workspace Platform Detection — Design Spec

*Cross-platform tmux support for Atelier workspace commands.*

---

## Section 1 — Core concept and scope

Atelier's workspace commands currently use `libtmux` directly, which requires a local tmux server. On Windows, tmux is not natively available. This spec adds platform detection and a WSL bridge so workspace commands work on Windows via WSL, while macOS and Linux continue using tmux directly.

**Approach:** Thin abstraction layer. A new `scripts/preflight.py` handles platform detection and tmux availability. `workspace.py` calls preflight once at import time and routes tmux operations through a `_run_tmux()` helper that issues either libtmux calls (macOS/Linux) or `wsl -- tmux` subprocess calls (Windows).

**Scope:**
- `scripts/preflight.py` — new file
- `scripts/workspace.py` — modified to use preflight + `_run_tmux()` on Windows
- `tests/test_preflight.py` — new test file
- `tests/test_workspace.py` — extended with Windows-path tests

---

## Section 2 — `scripts/preflight.py`

### Public API

```python
class PreflightError(Exception):
    pass

def check() -> None
def get_tmux_cmd() -> list[str]
```

### `check()` behavior

Called once at `workspace.py` import time. Raises `PreflightError` if the environment is unusable.

**Windows path:**
1. Run `wsl --status` — if it fails, raise `PreflightError`:
   > "Workspace commands require WSL on Windows. Please install WSL first: https://aka.ms/wsl"
2. Determine distro: use `ATELIER_WSL_DISTRO` env var if set, otherwise use WSL default (no `-d` flag)
3. Run `wsl [--d <distro>] -- tmux -V` — if it fails, prompt user:
   > "tmux is not installed in your WSL distro. Install it now? (y/n)"
   - Yes → run `wsl [--d <distro>] -- sudo apt-get install -y tmux`
   - No → raise `PreflightError`: "tmux is required. Install it manually: `sudo apt-get install tmux`"

**macOS path:**
1. Run `tmux -V` — if it fails, prompt user:
   > "tmux is not installed. Install it now? (y/n)"
   - Yes → run `brew install tmux`
   - No → raise `PreflightError`: "tmux is required. Install it manually: `brew install tmux`"

**Linux path:**
1. Run `tmux -V` — if it fails, prompt user:
   > "tmux is not installed. Install it now? (y/n)"
   - Yes → detect package manager and run appropriate installer:
     - apt: `sudo apt-get install -y tmux`
     - dnf: `sudo dnf install -y tmux`
     - pacman: `sudo pacman -S --noconfirm tmux`
   - No → raise `PreflightError`: "tmux is required. Install it with your package manager."

### `get_tmux_cmd()` behavior

Returns the base command list for tmux invocation:
- Windows: `["wsl", "--", "tmux"]` or `["wsl", "-d", "<distro>", "--", "tmux"]`
- macOS/Linux: `["tmux"]`

---

## Section 3 — `workspace.py` changes

### Platform routing

```python
import sys
import subprocess
from scripts import preflight

if sys.platform != "win32":
    import libtmux

preflight.check()  # called once at import time

def _run_tmux(args: list[str]) -> subprocess.CompletedProcess:
    cmd = preflight.get_tmux_cmd() + args
    return subprocess.run(cmd, capture_output=True, text=True, check=True)
```

### macOS/Linux path

Unchanged — libtmux used directly for all operations. `_run_tmux()` not called.

### Windows path

All workspace operations use `_run_tmux()` with raw tmux CLI arguments. Return types are plain `dict` with the same fields as libtmux objects (name, id, etc.) so callers see no API difference.

Examples:
- `create_workspace` → `_run_tmux(["new-session", "-d", "-s", name, "-c", project_root])`
- `list_workspaces` → `_run_tmux(["list-sessions", "-F", "#{session_name}"])`
- `create_room` → `_run_tmux(["new-window", "-t", workspace, "-n", room_name])`
- `agent_join` → `_run_tmux(["split-window", "-t", f"{workspace}:{room_name}"])`

The `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` environment variable is passed via `wsl -- env CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 tmux ...` on Windows.

---

## Section 4 — Testing

### `tests/test_preflight.py` (new)

All tests mock `sys.platform` and `subprocess.run`. No live tmux or WSL required.

- `test_check_linux_tmux_present` — tmux found, check passes
- `test_check_linux_tmux_missing_user_accepts` — tmux missing, user says y, apt-get called
- `test_check_linux_tmux_missing_user_declines` — tmux missing, user says n, PreflightError raised
- `test_check_macos_tmux_missing_user_accepts` — brew install called
- `test_check_windows_wsl_missing` — wsl --status fails, PreflightError raised
- `test_check_windows_tmux_missing_user_accepts` — wsl apt-get install called
- `test_check_windows_wsl_distro_env_var` — ATELIER_WSL_DISTRO respected
- `test_get_tmux_cmd_linux` — returns `["tmux"]`
- `test_get_tmux_cmd_windows` — returns `["wsl", "--", "tmux"]`
- `test_get_tmux_cmd_windows_with_distro` — returns `["wsl", "-d", "Ubuntu", "--", "tmux"]`
- `test_detect_package_manager_apt` — apt detected on Ubuntu
- `test_detect_package_manager_dnf` — dnf detected on Fedora
- `test_detect_package_manager_pacman` — pacman detected on Arch

### `tests/test_workspace.py` (extended)

Existing 14 tests unchanged. New tests added:

- `test_create_workspace_windows` — mock `sys.platform = "win32"`, verify `wsl -- tmux new-session` called
- `test_list_workspaces_windows` — verify `wsl -- tmux list-sessions` called
- `test_agent_join_windows` — verify `wsl -- tmux split-window` called with agent teams env var

---

## Section 5 — File storage

| Path | Purpose |
|---|---|
| `scripts/preflight.py` | Platform detection + tmux install |
| `scripts/workspace.py` | Modified — adds `_run_tmux()` + Windows path |
| `tests/test_preflight.py` | Preflight tests (13 tests) |
| `tests/test_workspace.py` | Extended with 3 Windows-path tests |

---

## Section 6 — Constraints

- `ATELIER_WSL_DISTRO` env var overrides WSL distro selection; unset = WSL default
- Auto-install only after explicit user confirmation
- All tests stay fully mocked — no live tmux or WSL required
- macOS/Linux path unchanged — zero regression risk
- Public API of `workspace.py` unchanged — callers unaffected
