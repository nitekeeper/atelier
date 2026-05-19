"""Platform detection and preflight checks for Atelier workspace commands.

On Windows, workspace commands run tmux via WSL. On macOS/Linux, tmux runs
natively. This module centralizes that decision so callers can ask for the
right tmux invocation without branching on platform themselves.
"""

import os
import subprocess
import sys
from shutil import which


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
        return [*_wsl_base_cmd(), "tmux"]
    return ["tmux"]


def _prompt(msg: str) -> bool:
    if not sys.stdin.isatty():
        raise PreflightError(
            f"{msg} — non-interactive environment detected. Install tmux manually and re-run."
        )
    answer = input(f"{msg} (y/n): ").strip().lower()
    return answer == "y"


def _detect_linux_package_manager() -> str | None:
    for pm in ("apt-get", "dnf", "pacman"):
        if which(pm):
            return pm
    return None


def _install_tmux_linux() -> None:
    pm = _detect_linux_package_manager()
    try:
        if pm == "apt-get":
            subprocess.run(["sudo", "apt-get", "install", "-y", "tmux"], check=True)
        elif pm == "dnf":
            subprocess.run(["sudo", "dnf", "install", "-y", "tmux"], check=True)
        elif pm == "pacman":
            subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "tmux"], check=True)
        else:
            raise PreflightError("Could not detect package manager. Install tmux manually.")
    except subprocess.CalledProcessError as e:
        raise PreflightError(f"Failed to install tmux: {e}") from e


def _check_windows() -> None:
    try:
        result = subprocess.run(["wsl", "--status"], capture_output=True, timeout=10)
    except FileNotFoundError as e:
        raise PreflightError(
            "Workspace commands require WSL on Windows. "
            "Please install WSL first: https://aka.ms/wsl"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise PreflightError("WSL check timed out. Ensure WSL is running and try again.") from e
    if result.returncode != 0:
        raise PreflightError(
            "Workspace commands require WSL on Windows. "
            "Please install WSL first: https://aka.ms/wsl"
        )
    wsl_cmd = _wsl_base_cmd()
    try:
        result = subprocess.run([*wsl_cmd, "tmux", "-V"], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired as e:
        raise PreflightError("tmux check in WSL timed out.") from e
    if result.returncode != 0:
        if _prompt("tmux is not installed in your WSL distro. Install it now? (uses apt-get)"):
            try:
                subprocess.run([*wsl_cmd, "sudo", "apt-get", "install", "-y", "tmux"], check=True)
            except subprocess.CalledProcessError as e:
                raise PreflightError(f"Failed to install tmux in WSL: {e}") from e
        else:
            raise PreflightError("tmux is required. Install it manually: sudo apt-get install tmux")


def _check_macos() -> None:
    try:
        result = subprocess.run(["tmux", "-V"], capture_output=True, timeout=10)
        tmux_missing = result.returncode != 0
    except FileNotFoundError:
        tmux_missing = True
    except subprocess.TimeoutExpired:
        tmux_missing = True
    if tmux_missing:
        if _prompt("tmux is not installed. Install it now?"):
            try:
                subprocess.run(["brew", "install", "tmux"], check=True)
            except FileNotFoundError as e:
                raise PreflightError(
                    "Homebrew not found. Install tmux manually: https://brew.sh"
                ) from e
            except subprocess.CalledProcessError as e:
                raise PreflightError(f"Failed to install tmux via brew: {e}") from e
        else:
            raise PreflightError("tmux is required. Install it manually: brew install tmux")


def _check_linux() -> None:
    try:
        result = subprocess.run(["tmux", "-V"], capture_output=True, timeout=10)
        tmux_missing = result.returncode != 0
    except FileNotFoundError:
        tmux_missing = True
    except subprocess.TimeoutExpired:
        tmux_missing = True
    if tmux_missing:
        if _prompt("tmux is not installed. Install it now?"):
            _install_tmux_linux()
        else:
            raise PreflightError("tmux is required. Install it with your package manager.")


def check() -> None:
    """Run platform-appropriate preflight checks. Raises PreflightError on failure."""
    if sys.platform == "win32":
        _check_windows()
    elif sys.platform == "darwin":
        _check_macos()
    else:
        _check_linux()
