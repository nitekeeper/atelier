"""Tests for scripts/platform_utils.py."""
import os
import stat
import sys

import pytest

from scripts.platform_utils import is_linux, is_macos, is_windows, safe_rmtree


class TestPlatformPredicates:
    def test_exactly_one_predicate_true(self):
        """Exactly one of the three predicates must match the current platform."""
        assert sum([is_windows(), is_macos(), is_linux()]) == 1

    def test_predicates_match_sys_platform(self):
        if sys.platform == "win32":
            assert is_windows() and not is_macos() and not is_linux()
        elif sys.platform == "darwin":
            assert is_macos() and not is_windows() and not is_linux()
        elif sys.platform.startswith("linux"):
            assert is_linux() and not is_windows() and not is_macos()


class TestSafeRmtree:
    def test_removes_normal_directory(self, tmp_path):
        d = tmp_path / "tree"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "a.txt").write_text("a")
        (d / "b.txt").write_text("b")
        safe_rmtree(d)
        assert not d.exists()

    def test_noop_when_missing(self, tmp_path):
        safe_rmtree(tmp_path / "does-not-exist")  # must not raise

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only readonly attribute")
    def test_removes_readonly_files_on_windows(self, tmp_path):
        d = tmp_path / "readonly_tree"
        d.mkdir()
        f = d / "locked.txt"
        f.write_text("locked")
        os.chmod(f, stat.S_IREAD)
        safe_rmtree(d)
        assert not d.exists()
