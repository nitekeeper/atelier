import os
import pytest
from unittest.mock import MagicMock


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
    run_mock = mocker.patch("subprocess.run")
    run_mock.side_effect = [
        MagicMock(returncode=1),   # tmux -V fails
        MagicMock(returncode=0),   # which apt-get succeeds
        MagicMock(returncode=0),   # apt-get install succeeds
    ]
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    mocker.patch("scripts.preflight._prompt", return_value=True)
    pf.check()  # should not raise


def test_check_linux_tmux_missing_user_declines(mocker):
    mocker.patch("sys.platform", "linux")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=1))
    from importlib import reload
    import scripts.preflight as pf
    reload(pf)
    mocker.patch("scripts.preflight._prompt", return_value=False)
    with pytest.raises(pf.PreflightError, match="package manager"):
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
