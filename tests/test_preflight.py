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
