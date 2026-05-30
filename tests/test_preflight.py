import os
import subprocess
from unittest.mock import MagicMock

import pytest


def test_prompt_non_interactive_raises(mocker):
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("sys.stdin.isatty", return_value=False)
    with pytest.raises(pf.PreflightError, match="non-interactive"):
        pf._prompt("Install tmux now?")


def test_check_linux_tmux_timeout(mocker):
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("sys.platform", "linux")
    mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["tmux", "-V"], 10))
    mocker.patch("scripts.preflight._prompt", return_value=False)
    with pytest.raises(pf.PreflightError):
        pf.check()


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


def test_check_windows_wsl_not_on_path(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch("subprocess.run", side_effect=FileNotFoundError)
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    with pytest.raises(pf.PreflightError, match="WSL"):
        pf.check()


def test_check_linux_tmux_present(mocker):
    mocker.patch("sys.platform", "linux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    pf.check()  # should not raise


def test_check_linux_tmux_missing_user_accepts(mocker):
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),  # tmux -V fails
        MagicMock(returncode=0),  # apt-get install succeeds
    ]
    mocker.patch(
        "scripts.preflight.which",
        side_effect=lambda pm: "/usr/bin/apt-get" if pm == "apt-get" else None,
    )
    mocker.patch("scripts.preflight._prompt", return_value=True)
    pf.check()  # should not raise


def test_check_linux_tmux_missing_user_declines(mocker):
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=1))
    mocker.patch("scripts.preflight._prompt", return_value=False)
    with pytest.raises(pf.PreflightError, match="tmux is required"):
        pf.check()


def test_check_macos_tmux_missing_user_accepts(mocker):
    mocker.patch("sys.platform", "darwin")
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),  # tmux -V fails
        MagicMock(returncode=0),  # brew install succeeds
    ]
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight._prompt", return_value=True)
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
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight._prompt", return_value=True)
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


def test_check_linux_tmux_not_on_path_user_accepts(mocker):
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        FileNotFoundError,  # tmux -V not on PATH
        MagicMock(returncode=0),  # apt-get install succeeds
    ]
    mocker.patch(
        "scripts.preflight.which",
        side_effect=lambda pm: "/usr/bin/apt-get" if pm == "apt-get" else None,
    )
    mocker.patch("scripts.preflight._prompt", return_value=True)
    pf.check()  # should not raise


def test_check_linux_tmux_not_on_path_user_declines(mocker):
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("subprocess.run", side_effect=FileNotFoundError)
    mocker.patch("scripts.preflight._prompt", return_value=False)
    with pytest.raises(pf.PreflightError, match="tmux is required"):
        pf.check()


def test_check_macos_tmux_not_on_path_user_accepts(mocker):
    mocker.patch("sys.platform", "darwin")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        FileNotFoundError,  # tmux -V not on PATH
        MagicMock(returncode=0),  # brew install succeeds
    ]
    mocker.patch("scripts.preflight._prompt", return_value=True)
    pf.check()  # should not raise


def test_check_macos_tmux_not_on_path_user_declines(mocker):
    mocker.patch("sys.platform", "darwin")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("subprocess.run", side_effect=FileNotFoundError)
    mocker.patch("scripts.preflight._prompt", return_value=False)
    with pytest.raises(pf.PreflightError, match="tmux is required"):
        pf.check()


def test_check_macos_brew_not_installed(mocker):
    mocker.patch("sys.platform", "darwin")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        FileNotFoundError,  # tmux -V not on PATH
        FileNotFoundError,  # brew install -> brew not on PATH
    ]
    mocker.patch("scripts.preflight._prompt", return_value=True)
    with pytest.raises(pf.PreflightError, match="Homebrew not found"):
        pf.check()


def test_detect_package_manager_apt(mocker):
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch(
        "scripts.preflight.which",
        side_effect=lambda pm: "/usr/bin/apt-get" if pm == "apt-get" else None,
    )
    result = pf._detect_linux_package_manager()
    assert result == "apt-get"


def test_detect_package_manager_dnf(mocker):
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch(
        "scripts.preflight.which", side_effect=lambda pm: "/usr/bin/dnf" if pm == "dnf" else None
    )
    result = pf._detect_linux_package_manager()
    assert result == "dnf"


def test_detect_package_manager_pacman(mocker):
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch(
        "scripts.preflight.which",
        side_effect=lambda pm: "/usr/bin/pacman" if pm == "pacman" else None,
    )
    result = pf._detect_linux_package_manager()
    assert result == "pacman"


# ── tmux_available (atelier#62 Part D — non-raising yes/no availability) ─────


def test_tmux_available_false_when_which_returns_none(mocker):
    """which('tmux') -> None on a native platform => False, no server probe."""
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight.which", return_value=None)
    run_mock = mocker.patch("subprocess.run")
    assert pf.tmux_available() is False
    # When the binary is absent we must NOT even reach the server probe.
    run_mock.assert_not_called()


def test_tmux_available_true_when_which_and_probe_ok(mocker):
    """which finds tmux AND the server probe returns rc 0 => True."""
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight.which", return_value="/usr/bin/tmux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    assert pf.tmux_available() is True


def test_tmux_available_false_when_probe_returns_nonzero(mocker):
    """which finds tmux but the server probe returns rc != 0 => False."""
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight.which", return_value="/usr/bin/tmux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=1))
    assert pf.tmux_available() is False


def test_tmux_available_false_when_probe_raises(mocker):
    """A probe that RAISES (timeout / FileNotFoundError / OSError) must
    collapse to False — tmux_available NEVER propagates an exception."""
    mocker.patch("sys.platform", "linux")
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    mocker.patch("scripts.preflight.which", return_value="/usr/bin/tmux")
    for exc in (
        subprocess.TimeoutExpired(["tmux", "start-server"], 10),
        FileNotFoundError(),
        OSError("boom"),
    ):
        mocker.patch("subprocess.run", side_effect=exc)
        assert pf.tmux_available() is False


def test_tmux_available_windows_skips_which_uses_probe(mocker):
    """On Windows the real tmux is inside WSL (not on host PATH), so
    tmux_available must NOT short-circuit on which('tmux') -> None; it relies
    on the WSL-wrapped server probe (get_tmux_cmd prefix)."""
    mocker.patch("sys.platform", "win32")
    mocker.patch.dict(os.environ, {}, clear=True)
    from importlib import reload

    import scripts.preflight as pf

    reload(pf)
    # which would return None for tmux on the Windows host — but we must not
    # gate on it. Force None to prove the Windows path ignores it.
    mocker.patch("scripts.preflight.which", return_value=None)
    run_mock = mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    assert pf.tmux_available() is True
    # The probe was actually invoked (which-None did NOT short-circuit), and it
    # used the WSL prefix.
    run_mock.assert_called_once()
    invoked = run_mock.call_args.args[0]
    assert invoked[:2] == ["wsl", "--"]
    assert "tmux" in invoked
