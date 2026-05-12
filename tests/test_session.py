# tests/test_session.py
import pytest
from pathlib import Path
from scripts.session import read_session, write_session

def test_write_creates_file(tmp_path):
    work_md = tmp_path / "work.md"
    write_session(work_md, {
        "current_task": "Build DB schema",
        "status": "in-progress",
        "blocking_reason": "",
        "last_session": "2026-05-12",
        "accomplished": "Created migration files",
        "next_action": "Write DB helper",
    })
    assert work_md.exists()

def test_read_returns_dict(tmp_path):
    work_md = tmp_path / "work.md"
    data = {
        "current_task": "Build DB schema",
        "status": "in-progress",
        "blocking_reason": "",
        "last_session": "2026-05-12",
        "accomplished": "Created migration files",
        "next_action": "Write DB helper",
    }
    write_session(work_md, data)
    result = read_session(work_md)
    assert result["current_task"] == "Build DB schema"
    assert result["status"] == "in-progress"
    assert result["next_action"] == "Write DB helper"

def test_write_overwrites_existing(tmp_path):
    work_md = tmp_path / "work.md"
    write_session(work_md, {"current_task": "old", "status": "complete",
                            "blocking_reason": "", "last_session": "2026-05-11",
                            "accomplished": "old work", "next_action": "nothing"})
    write_session(work_md, {"current_task": "new", "status": "in-progress",
                            "blocking_reason": "", "last_session": "2026-05-12",
                            "accomplished": "new work", "next_action": "do more"})
    result = read_session(work_md)
    assert result["current_task"] == "new"

def test_read_missing_file_returns_none(tmp_path):
    work_md = tmp_path / "work.md"
    assert read_session(work_md) is None
