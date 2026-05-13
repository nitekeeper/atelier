# tests/test_workspace.py
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
    mocker.patch("sys.platform", "linux")

# ── workspace tests ──────────────────────────────────────────────────────────

def test_create_workspace_creates_tmux_session(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_session.name = "my-project"
    mock_server.new_session.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    session = create_workspace(name="my-project", project_root="/home/user/project")

    mock_server.new_session.assert_called_once_with(
        session_name="my-project",
        start_directory="/home/user/project",
        environment={AGENT_TEAMS_ENV: "1"}
    )
    assert session.name == "my-project"

def test_create_workspace_creates_main_room(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_server.new_session.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    create_workspace(name="my-project", project_root="/home/user/project")

    mock_session.new_window.assert_called_once_with(window_name="main")

def test_list_workspaces_returns_session_names(mocker):
    mock_server = MagicMock()
    s1, s2 = MagicMock(), MagicMock()
    s1.name, s2.name = "project-a", "project-b"
    mock_server.sessions = [s1, s2]
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    names = list_workspaces()
    assert names == ["project-a", "project-b"]

def test_join_workspace_attaches_to_session(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_session.name = "my-project"
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    session = join_workspace("my-project")

    mock_server.find_where.assert_called_once_with({"session_name": "my-project"})
    mock_session.attach_session.assert_called_once()
    assert session.name == "my-project"

def test_join_workspace_raises_if_not_found(mocker):
    mock_server = MagicMock()
    mock_server.find_where.return_value = None
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    with pytest.raises(ValueError, match="Workspace 'unknown' not found"):
        join_workspace("unknown")

def test_leave_workspace_detaches(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    leave_workspace("my-project")
    mock_session.detach_session.assert_called_once()

# ── room tests ───────────────────────────────────────────────────────────────

def test_create_room_adds_window(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_window.name = "standup"
    mock_server.find_where.return_value = mock_session
    mock_session.new_window.return_value = mock_window
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    window = create_room(workspace="my-project", room_name="standup")

    mock_session.new_window.assert_called_once_with(window_name="standup")
    assert window.name == "standup"

def test_list_rooms_returns_window_names(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    w1, w2 = MagicMock(), MagicMock()
    w1.name, w2.name = "main", "standup"
    mock_session.windows = [w1, w2]
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    rooms = list_rooms("my-project")
    assert rooms == ["main", "standup"]

def test_join_room_selects_window(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_session.find_where.return_value = mock_window
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    join_room(workspace="my-project", room_name="standup")

    mock_session.find_where.assert_called_once_with({"window_name": "standup"})
    mock_window.select_window.assert_called_once()

def test_join_room_raises_if_not_found(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_session.find_where.return_value = None
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    with pytest.raises(ValueError, match="Room 'ghost' not found"):
        join_room(workspace="my-project", room_name="ghost")

def test_close_room_kills_window(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_session.find_where.return_value = mock_window
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    close_room(workspace="my-project", room_name="standup")
    mock_window.kill_window.assert_called_once()

def test_close_main_room_raises(mocker):
    mocker.patch("scripts.workspace._get_server")
    with pytest.raises(ValueError, match="Cannot close the main room"):
        close_room(workspace="my-project", room_name="main")

# ── agent desk tests ─────────────────────────────────────────────────────────

def test_agent_join_creates_pane(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_pane = MagicMock()
    mock_session.find_where.return_value = mock_window
    mock_window.split_window.return_value = mock_pane
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    pane = agent_join(workspace="my-project", room_name="main", agent_id="dev-1")

    mock_window.split_window.assert_called_once_with(attach=False)
    mock_pane.send_keys.assert_called_once_with("claude")
    assert pane == mock_pane

def test_agent_leave_closes_pane(mocker):
    mock_server = MagicMock()
    mock_session = MagicMock()
    mock_window = MagicMock()
    mock_pane = MagicMock()
    mock_pane.id = "%3"
    mock_window.panes = [mock_pane]
    mock_session.find_where.return_value = mock_window
    mock_server.find_where.return_value = mock_session
    mocker.patch("scripts.workspace._get_server", return_value=mock_server)

    agent_leave(workspace="my-project", room_name="main", pane_id="%3")
    mock_pane.kill_pane.assert_called_once()

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
