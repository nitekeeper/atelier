"""Tests for scripts/git_utils.py."""

import subprocess

import pytest

from scripts.git_utils import git


class TestGitHelper:
    def test_basic_success(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        result = git(["rev-parse", "--git-dir"], tmp_path)
        assert result.returncode == 0
        assert ".git" in result.stdout

    def test_check_true_raises_on_failure(self, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            git(["status"], tmp_path)  # not a git repo

    def test_check_false_does_not_raise(self, tmp_path):
        result = git(["status"], tmp_path, check=False)
        assert result.returncode != 0

    def test_errors_kwarg_passed_through(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        result = git(["rev-parse", "--git-dir"], tmp_path, errors="replace")
        assert result.returncode == 0
