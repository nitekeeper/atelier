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
        session_name=name, start_directory=project_root, environment={AGENT_TEAMS_ENV: "1"}
    )
    session.new_window(window_name="main")
    return session


def list_workspaces() -> list[str]:
    if sys.platform == "win32":
        result = subprocess.run(
            preflight.get_tmux_cmd() + ["list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
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
        result = subprocess.run(
            preflight.get_tmux_cmd() + ["list-windows", "-t", workspace, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
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
        count_result = _run_tmux(
            ["list-panes", "-t", f"{workspace}:{room_name}", "-F", "#{pane_id}"]
        )
        pane_count = len([p for p in count_result.stdout.strip().split("\n") if p])
        if pane_count >= _MAX_AGENTS:
            raise ValueError(f"Maximum {_MAX_AGENTS} agents per room reached")
        result = _run_tmux(
            [
                "split-window",
                "-t",
                f"{workspace}:{room_name}",
                "-e",
                f"{AGENT_TEAMS_ENV}=1",
                "-P",
                "-F",
                "#{pane_id}",
            ]
        )
        pane_id = result.stdout.strip()
        _run_tmux(["send-keys", "-t", pane_id, "claude", "Enter"])
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


# ---------------------------------------------------------------------------
# Workspace-root resolution (Plan 1 Task 7)
#
# Plan 3 `scripts/documents.py:create_document` imports `workspace_root` from
# this module to locate the on-disk markdown file at `workspace_root() /
# filename` before passing the body to `backend.write_document` (spec §6.8).
# Spec §10.2's `resolve_scope()` uses `find_git_root` from `scripts.git_utils`
# to derive workspace identity.
# ---------------------------------------------------------------------------

# NOTE: imports are mid-file because plan Task 7 specified APPEND. Plan 3
# cleanup will consolidate them with the module's top-level imports.
from pathlib import Path  # noqa: E402  (deliberately co-located with helper)

from scripts.git_utils import find_git_root  # noqa: E402


def workspace_root() -> Path:
    """Resolve the workspace root (= git root) for the current process.

    Plan 3's `scripts/documents.py:create_document` uses this to locate the
    on-disk markdown file at `workspace_root() / filename` before passing
    its body to `backend.write_document` (spec §6.8).

    Raises FileNotFoundError when CWD isn't inside a git repository —
    workspace-less callers (rare; mainly daily-log writes) must catch and
    skip the workspace-bound path, or operate under explicit
    `workspace_id=None` semantics per spec §10.4.
    """
    cwd = Path.cwd().resolve()
    root = find_git_root(cwd)
    if root is None:
        raise FileNotFoundError(
            f"not inside a git repository: {cwd}. "
            f"workspace_root() requires CWD to be under a git workspace."
        )
    return root


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
        pane = agent_join(
            workspace=args.workspace, room_name=args.room_name, agent_id=args.agent_id
        )
        print(
            f"Agent '{args.agent_id}' joined room '{args.room_name}' (pane: {pane.id}). Claude Code launched."
        )

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
