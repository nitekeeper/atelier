# Workspace Platform Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-platform tmux support to Atelier workspace commands — native tmux on macOS/Linux, tmux-via-WSL on Windows, with automatic detection and install prompts.

**Architecture:** A new `scripts/preflight.py` handles platform detection, WSL availability, and tmux installation. `scripts/workspace.py` calls `preflight.check()` once at import time and routes all tmux operations through a `_run_tmux()` helper that issues either libtmux calls (macOS/Linux) or `wsl -- tmux` subprocess calls (Windows).

**Tech Stack:** Python 3.11+ stdlib only (`sys`, `os`, `subprocess`); `libtmux` on macOS/Linux; `wsl` CLI on Windows.

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `scripts/preflight.py` | Create | Platform detection + tmux install |
| `scripts/workspace.py` | Modify | Add `_run_tmux()` + Windows path |
| `tests/test_preflight.py` | Create | 13 preflight tests |
| `tests/test_workspace.py` | Modify | Add 3 Windows-path tests |

---

### Task 0: `scripts/preflight.py` — platform detection and WSL check

**Files:**
- Create: `scripts/preflight.py`
- Create: `tests/test_preflight.py`

- [ ] **Step 1: Write failing tests for platform detection and WSL check**

Create `tests/test_preflight.py`:

```python
# tests/test_preflight.py
import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock


def test_get_tmux_cmd_linux(mocker):
    mocker.patch("sys.platform", "linux")
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    assert pf.get_tmux_cmd() == ["tmux"]


def test_get_tmux_cmd_macos(mocker):
    mocker.patch("sys.platform", "darwin")
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    assert pf.get_tmux_cmd() == ["tmux"]


def test_get_tmux_cmd_windows_default(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.dict(os.environ, {}, clear=True)
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    assert pf.get_tmux_cmd() == ["wsl", "--", "tmux"]


def test_get_tmux_cmd_windows_with_distro(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.dict(os.environ, {"ATELIER_WSL_DISTRO": "Ubuntu"})
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    assert pf.get_tmux_cmd() == ["wsl", "-d", "Ubuntu", "--", "tmux"]


def test_check_windows_wsl_missing(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=1))
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    with pytest.raises(pf.PreflightError, match="WSL"):
        pf.check()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_preflight.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` — `scripts/preflight.py` does not exist yet.

- [ ] **Step 3: Create `scripts/preflight.py` with platform detection and WSL check**

```python
# scripts/preflight.py
import os
import subprocess
import sys


class PreflightError(Exception):
    pass


def _get_wsl_distro() -> str | None:
    return os.environ.get("ATELIER_WSL_DISTRO")


def _wsl_base_cmd() -> list[str]:
    distro = _get_wsl_distro()
    if distro:
        return ["wsl", "-d", distro, "--"]
    return ["wsl", "--"]


def get_tmux_cmd() -> list[str]:
    if sys.platform == "win32":
        return _wsl_base_cmd() + ["tmux"]
    return ["tmux"]


def _prompt(msg: str) -> bool:
    answer = input(f"{msg} (y/n): ").strip().lower()
    return answer == "y"


def _check_windows() -> None:
    result = subprocess.run(["wsl", "--status"], capture_output=True)
    if result.returncode != 0:
        raise PreflightError(
            "Workspace commands require WSL on Windows. "
            "Please install WSL first: https://aka.ms/wsl"
        )


def _check_macos() -> None:
    pass  # tmux check added in Task 1


def _check_linux() -> None:
    pass  # tmux check added in Task 1


def check() -> None:
    if sys.platform == "win32":
        _check_windows()
    elif sys.platform == "darwin":
        _check_macos()
    else:
        _check_linux()
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_preflight.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/preflight.py tests/test_preflight.py
git commit -m "feat: add preflight.py — platform detection and WSL check"
```

---

### Task 1: `scripts/preflight.py` — tmux check and auto-install

**Files:**
- Modify: `scripts/preflight.py`
- Modify: `tests/test_preflight.py`

- [ ] **Step 1: Write failing tests for tmux check and auto-install**

Append to `tests/test_preflight.py`:

```python
def test_check_linux_tmux_present(mocker):
    mocker.patch("sys.platform", "linux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    pf.check()  # should not raise


def test_check_linux_tmux_missing_user_accepts(mocker):
    mocker.patch("sys.platform", "linux")
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),   # tmux -V fails
        MagicMock(returncode=0),   # which apt-get succeeds
        MagicMock(returncode=0),   # apt-get install succeeds
    ]
    mocker.patch("scripts.preflight._prompt", return_value=True)
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    pf.check()  # should not raise


def test_check_linux_tmux_missing_user_declines(mocker):
    mocker.patch("sys.platform", "linux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=1))
    mocker.patch("scripts.preflight._prompt", return_value=False)
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    with pytest.raises(pf.PreflightError, match="package manager"):
        pf.check()


def test_check_macos_tmux_missing_user_accepts(mocker):
    mocker.patch("sys.platform", "darwin")
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),  # tmux -V fails
        MagicMock(returncode=0),  # brew install succeeds
    ]
    mocker.patch("scripts.preflight._prompt", return_value=True)
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    pf.check()  # should not raise


def test_check_windows_tmux_missing_user_accepts(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.dict(os.environ, {}, clear=True)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=0),  # wsl --status passes
        MagicMock(returncode=1),  # wsl -- tmux -V fails
        MagicMock(returncode=0),  # wsl -- apt-get install succeeds
    ]
    mocker.patch("scripts.preflight._prompt", return_value=True)
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    pf.check()  # should not raise


def test_check_windows_wsl_distro_env_var(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.dict(os.environ, {"ATELIER_WSL_DISTRO": "Ubuntu"})
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=0),  # wsl --status passes
        MagicMock(returncode=0),  # wsl -d Ubuntu -- tmux -V passes
    ]
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    pf.check()
    calls = [str(c) for c in run_mock.call_args_list]
    assert any("Ubuntu" in c for c in calls)


def test_detect_package_manager_apt(mocker):
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.return_value = MagicMock(returncode=0)
    result = pf._detect_linux_package_manager()
    assert result == "apt-get"


def test_detect_package_manager_dnf(mocker):
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),  # apt-get not found
        MagicMock(returncode=0),  # dnf found
    ]
    result = pf._detect_linux_package_manager()
    assert result == "dnf"


def test_detect_package_manager_pacman(mocker):
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),  # apt-get not found
        MagicMock(returncode=1),  # dnf not found
        MagicMock(returncode=0),  # pacman found
    ]
    result = pf._detect_linux_package_manager()
    assert result == "pacman"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_preflight.py -v
```

Expected: 5 existing pass, ~8 new tests fail — `_detect_linux_package_manager` not defined, `_check_linux` / `_check_macos` are stubs.

- [ ] **Step 3: Implement tmux check and auto-install in `scripts/preflight.py`**

Replace the full file contents:

```python
# scripts/preflight.py
import os
import subprocess
import sys


class PreflightError(Exception):
    pass


def _get_wsl_distro() -> str | None:
    return os.environ.get("ATELIER_WSL_DISTRO")


def _wsl_base_cmd() -> list[str]:
    distro = _get_wsl_distro()
    if distro:
        return ["wsl", "-d", distro, "--"]
    return ["wsl", "--"]


def get_tmux_cmd() -> list[str]:
    if sys.platform == "win32":
        return _wsl_base_cmd() + ["tmux"]
    return ["tmux"]


def _prompt(msg: str) -> bool:
    answer = input(f"{msg} (y/n): ").strip().lower()
    return answer == "y"


def _detect_linux_package_manager() -> str | None:
    for pm in ["apt-get", "dnf", "pacman"]:
        result = subprocess.run(["which", pm], capture_output=True)
        if result.returncode == 0:
            return pm
    return None


def _install_tmux_linux() -> None:
    pm = _detect_linux_package_manager()
    if pm == "apt-get":
        subprocess.run(["sudo", "apt-get", "install", "-y", "tmux"], check=True)
    elif pm == "dnf":
        subprocess.run(["sudo", "dnf", "install", "-y", "tmux"], check=True)
    elif pm == "pacman":
        subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "tmux"], check=True)
    else:
        raise PreflightError(
            "Could not detect package manager. Install tmux manually."
        )


def _check_windows() -> None:
    result = subprocess.run(["wsl", "--status"], capture_output=True)
    if result.returncode != 0:
        raise PreflightError(
            "Workspace commands require WSL on Windows. "
            "Please install WSL first: https://aka.ms/wsl"
        )
    wsl_cmd = _wsl_base_cmd()
    result = subprocess.run(wsl_cmd + ["tmux", "-V"], capture_output=True)
    if result.returncode != 0:
        if _prompt("tmux is not installed in your WSL distro. Install it now?"):
            subprocess.run(
                wsl_cmd + ["sudo", "apt-get", "install", "-y", "tmux"], check=True
            )
        else:
            raise PreflightError(
                "tmux is required. Install it manually: sudo apt-get install tmux"
            )


def _check_macos() -> None:
    result = subprocess.run(["tmux", "-V"], capture_output=True)
    if result.returncode != 0:
        if _prompt("tmux is not installed. Install it now?"):
            subprocess.run(["brew", "install", "tmux"], check=True)
        else:
            raise PreflightError(
                "tmux is required. Install it manually: brew install tmux"
            )


def _check_linux() -> None:
    result = subprocess.run(["tmux", "-V"], capture_output=True)
    if result.returncode != 0:
        if _prompt("tmux is not installed. Install it now?"):
            _install_tmux_linux()
        else:
            raise PreflightError(
                "tmux is required. Install it with your package manager."
            )


def check() -> None:
    if sys.platform == "win32":
        _check_windows()
    elif sys.platform == "darwin":
        _check_macos()
    else:
        _check_linux()
```

- [ ] **Step 4: Run all preflight tests**

```
pytest tests/test_preflight.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Run full test suite to check for regressions**

```
pytest tests/ -v
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/preflight.py tests/test_preflight.py
git commit -m "feat: preflight — tmux check and auto-install for all platforms"
```

---

### Task 2: `scripts/workspace.py` — Windows path via `_run_tmux()`

**Files:**
- Modify: `scripts/workspace.py`
- Modify: `tests/test_workspace.py`

- [ ] **Step 1: Write failing tests for Windows workspace operations**

Append to `tests/test_workspace.py`:

```python
# ── Windows path tests ────────────────────────────────────────────────────────

def test_create_workspace_windows(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch("scripts.preflight.check")
    mocker.patch("scripts.preflight.get_tmux_cmd", return_value=["wsl", "--", "tmux"])
    run_mock = mocker.patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
    from importlib import reload
    import scripts.workspace as ws
    reload(ws)

    ws.create_workspace(name="my-project", project_root="/home/user/project")

    calls = [c.args[0] for c in run_mock.call_args_list]
    assert any("new-session" in c for c in calls)
    assert any("my-project" in c for c in calls)


def test_list_workspaces_windows(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch("scripts.preflight.check")
    mocker.patch("scripts.preflight.get_tmux_cmd", return_value=["wsl", "--", "tmux"])
    mocker.patch("subprocess.run", return_value=MagicMock(
        returncode=0, stdout="project-a\nproject-b\n"
    ))
    from importlib import reload
    import scripts.workspace as ws
    reload(ws)

    names = ws.list_workspaces()
    assert names == ["project-a", "project-b"]


def test_agent_join_windows(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch("scripts.preflight.check")
    mocker.patch("scripts.preflight.get_tmux_cmd", return_value=["wsl", "--", "tmux"])
    run_mock = mocker.patch("subprocess.run", return_value=MagicMock(
        returncode=0, stdout="%0\n"
    ))
    from importlib import reload
    import scripts.workspace as ws
    reload(ws)

    ws.agent_join(workspace="my-project", room_name="main", agent_id="dev-1")

    calls = [str(c) for c in run_mock.call_args_list]
    assert any("split-window" in c for c in calls)
    assert any("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in c for c in calls)
```

- [ ] **Step 2: Run new tests to verify they fail**

```
pytest tests/test_workspace.py::test_create_workspace_windows tests/test_workspace.py::test_list_workspaces_windows tests/test_workspace.py::test_agent_join_windows -v
```

Expected: 3 failures — `_run_tmux` not defined, no Windows path in functions.

- [ ] **Step 3: Rewrite `scripts/workspace.py` with Windows path**

Replace the full file:

```python
# scripts/workspace.py
import subprocess
import sys

from scripts import preflight

if sys.platform != "win32":
    import libtmux

AGENT_TEAMS_ENV = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
_MAX_AGENTS = 5

preflight.check()


class _Obj:
    """Simple return object for Windows path — matches libtmux attribute names."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _get_server() -> "libtmux.Server":
    return libtmux.Server()


def _run_tmux(args: list[str]) -> subprocess.CompletedProcess:
    cmd = preflight.get_tmux_cmd() + args
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def create_workspace(name: str, project_root: str):
    if sys.platform == "win32":
        _run_tmux(["new-session", "-d", "-s", name, "-c", project_root])
        _run_tmux(["setenv", "-t", name, AGENT_TEAMS_ENV, "1"])
        _run_tmux(["new-window", "-t", name, "-n", "main"])
        return _Obj(name=name)
    server = _get_server()
    session = server.new_session(
        session_name=name,
        start_directory=project_root,
        environment={AGENT_TEAMS_ENV: "1"}
    )
    session.new_window(window_name="main")
    return session


def list_workspaces() -> list[str]:
    if sys.platform == "win32":
        result = _run_tmux(["list-sessions", "-F", "#{session_name}"])
        return [s for s in result.stdout.strip().split("\n") if s]
    server = _get_server()
    return [s.name for s in server.sessions]


def join_workspace(name: str):
    if sys.platform == "win32":
        _run_tmux(["attach-session", "-t", name])
        return _Obj(name=name)
    server = _get_server()
    session = server.find_where({"session_name": name})
    if session is None:
        raise ValueError(f"Workspace '{name}' not found")
    session.attach_session()
    return session


def leave_workspace(name: str) -> None:
    if sys.platform == "win32":
        _run_tmux(["detach-client", "-s", name])
        return
    server = _get_server()
    session = server.find_where({"session_name": name})
    if session is None:
        raise ValueError(f"Workspace '{name}' not found")
    session.detach_session()


def create_room(workspace: str, room_name: str):
    if sys.platform == "win32":
        _run_tmux(["new-window", "-t", workspace, "-n", room_name])
        return _Obj(name=room_name)
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    return session.new_window(window_name=room_name)


def list_rooms(workspace: str) -> list[str]:
    if sys.platform == "win32":
        result = _run_tmux(["list-windows", "-t", workspace, "-F", "#{window_name}"])
        return [w for w in result.stdout.strip().split("\n") if w]
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    return [w.name for w in session.windows]


def join_room(workspace: str, room_name: str):
    if sys.platform == "win32":
        _run_tmux(["select-window", "-t", f"{workspace}:{room_name}"])
        return _Obj(name=room_name)
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    window = session.find_where({"window_name": room_name})
    if window is None:
        raise ValueError(f"Room '{room_name}' not found in workspace '{workspace}'")
    window.select_window()
    return window


def close_room(workspace: str, room_name: str) -> None:
    if room_name == "main":
        raise ValueError("Cannot close the main room")
    if sys.platform == "win32":
        _run_tmux(["kill-window", "-t", f"{workspace}:{room_name}"])
        return
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    window = session.find_where({"window_name": room_name})
    if window is None:
        raise ValueError(f"Room '{room_name}' not found")
    window.kill_window()


def agent_join(workspace: str, room_name: str, agent_id: str):
    if sys.platform == "win32":
        result = _run_tmux(["split-window", "-t", f"{workspace}:{room_name}", "-P", "-F", "#{pane_id}"])
        pane_id = result.stdout.strip()
        _run_tmux([
            "send-keys", "-t", pane_id,
            f"env {AGENT_TEAMS_ENV}=1 claude", "Enter"
        ])
        return _Obj(id=pane_id)
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    window = session.find_where({"window_name": room_name})
    if window is None:
        raise ValueError(f"Room '{room_name}' not found")
    if len(window.panes) >= _MAX_AGENTS:
        raise ValueError(f"Maximum {_MAX_AGENTS} agents per room reached")
    pane = window.split_window(attach=False)
    pane.send_keys("claude")
    return pane


def agent_leave(workspace: str, room_name: str, pane_id: str) -> None:
    if sys.platform == "win32":
        _run_tmux(["kill-pane", "-t", pane_id])
        return
    server = _get_server()
    session = server.find_where({"session_name": workspace})
    if session is None:
        raise ValueError(f"Workspace '{workspace}' not found")
    window = session.find_where({"window_name": room_name})
    if window is None:
        raise ValueError(f"Room '{room_name}' not found")
    pane = next((p for p in window.panes if p.id == pane_id), None)
    if pane is None:
        raise ValueError(f"Pane '{pane_id}' not found")
    pane.kill_pane()


if __name__ == "__main__":
    import sys
    import argparse

    cmd = sys.argv[1]

    if cmd == "workspace:create":
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--root", default=".")
        args = parser.parse_args(sys.argv[2:])
        session = create_workspace(name=args.name, project_root=args.root)
        print(f"Workspace '{session.name}' created with main room.")

    elif cmd == "workspace:list":
        workspaces = list_workspaces()
        if workspaces:
            for w in workspaces:
                print(f"  {w}")
        else:
            print("No active workspaces.")

    elif cmd == "workspace:join":
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        args = parser.parse_args(sys.argv[2:])
        join_workspace(args.name)
        print(f"Joined workspace '{args.name}'.")

    elif cmd == "workspace:leave":
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        args = parser.parse_args(sys.argv[2:])
        leave_workspace(args.name)
        print(f"Left workspace '{args.name}'.")

    elif cmd == "room:create":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        parser.add_argument("room_name")
        args = parser.parse_args(sys.argv[2:])
        create_room(workspace=args.workspace, room_name=args.room_name)
        print(f"Room '{args.room_name}' created in workspace '{args.workspace}'.")

    elif cmd == "room:list":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        args = parser.parse_args(sys.argv[2:])
        rooms = list_rooms(args.workspace)
        for r in rooms:
            print(f"  {r}")

    elif cmd == "room:join":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        parser.add_argument("room_name")
        args = parser.parse_args(sys.argv[2:])
        join_room(workspace=args.workspace, room_name=args.room_name)
        print(f"Joined room '{args.room_name}'.")

    elif cmd == "room:close":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        parser.add_argument("room_name")
        args = parser.parse_args(sys.argv[2:])
        close_room(workspace=args.workspace, room_name=args.room_name)
        print(f"Room '{args.room_name}' closed.")

    elif cmd == "agent:join":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        parser.add_argument("room_name")
        parser.add_argument("agent_id")
        args = parser.parse_args(sys.argv[2:])
        pane = agent_join(workspace=args.workspace, room_name=args.room_name, agent_id=args.agent_id)
        print(f"Agent '{args.agent_id}' joined room '{args.room_name}' (pane: {pane.id}). Claude Code launched.")

    elif cmd == "agent:leave":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace")
        parser.add_argument("room_name")
        parser.add_argument("pane_id")
        args = parser.parse_args(sys.argv[2:])
        agent_leave(workspace=args.workspace, room_name=args.room_name, pane_id=args.pane_id)
        print(f"Pane '{args.pane_id}' closed.")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
```

- [ ] **Step 4: Fix existing tests — mock `preflight.check` to prevent it running at import**

The existing 14 tests mock `_get_server` but `preflight.check()` now runs at module import time. Add a session-scoped autouse fixture at the top of `tests/test_workspace.py`, just after the imports:

```python
import pytest
from unittest.mock import MagicMock, patch, call
from scripts.workspace import (
    create_workspace, list_workspaces, join_workspace, leave_workspace,
    create_room, list_rooms, join_room, close_room,
    agent_join, agent_leave,
    AGENT_TEAMS_ENV
)

@pytest.fixture(autouse=True)
def mock_preflight(mocker):
    mocker.patch("scripts.preflight.check")
    mocker.patch("scripts.preflight.get_tmux_cmd", return_value=["tmux"])
```

- [ ] **Step 5: Run full test suite**

```
pytest tests/ -v
```

Expected: 80 existing + 13 preflight + 3 Windows workspace = **96 passed**.

- [ ] **Step 6: Commit**

```bash
git add scripts/workspace.py tests/test_workspace.py
git commit -m "feat: workspace — Windows path via wsl tmux subprocess"
```

---

## Self-Review

**Spec coverage:**
- ✅ Platform detection (`sys.platform`) — Task 0
- ✅ WSL check + PreflightError — Task 0
- ✅ `ATELIER_WSL_DISTRO` env var — Task 0 + Task 1
- ✅ tmux check + prompt + auto-install (Windows/macOS/Linux) — Task 1
- ✅ Package manager detection (apt/dnf/pacman) — Task 1
- ✅ `get_tmux_cmd()` returns correct command per platform — Task 0
- ✅ `_run_tmux()` helper routes to subprocess on Windows — Task 2
- ✅ `preflight.check()` called at import time — Task 2
- ✅ 13 preflight tests — Tasks 0 + 1
- ✅ 3 Windows workspace tests — Task 2
- ✅ Existing 14 workspace tests unaffected — autouse fixture in Task 2 Step 4

**Placeholder scan:** None found.

**Type consistency:**
- `_Obj` used consistently across all Windows return values in Task 2
- `preflight.check()` / `preflight.get_tmux_cmd()` called consistently in workspace.py
- `_wsl_base_cmd()` used in both `_check_windows()` and `get_tmux_cmd()` — consistent
