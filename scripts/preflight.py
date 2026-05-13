"""Platform detection and preflight checks for Atelier workspace commands.

On Windows, workspace commands run tmux via WSL. On macOS/Linux, tmux runs
natively. This module centralizes that decision so callers can ask for the
right tmux invocation without branching on platform themselves.
"""
import os
import subprocess
import sys


class PreflightError(Exception):
    """Raised when a required platform dependency is missing."""


def _get_wsl_distro() -> str | None:
    return os.environ.get("ATELIER_WSL_DISTRO")


def _wsl_base_cmd() -> list[str]:
    distro = _get_wsl_distro()
    if distro:
        return ["wsl", "-d", distro, "--"]
    return ["wsl", "--"]


def get_tmux_cmd() -> list[str]:
    """Return the command prefix used to invoke tmux on the current platform."""
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
    """Run platform-appropriate preflight checks. Raises PreflightError on failure."""
    if sys.platform == "win32":
        _check_windows()
    elif sys.platform == "darwin":
        _check_macos()
    else:
        _check_linux()
