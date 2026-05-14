# Atelier Self-Improve Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `dev:self-improve` skill — a user-initiated, multi-agent, meeting-driven cycle that analyzes Atelier's own codebase, reaches unanimous consensus on improvements, implements them in an isolated git clone, gates on tests, and auto-merges to main.

**Architecture:** A SKILL.md procedure drives the agentic phases (PM agenda, parallel pre-analysis, synthesis meeting, implementation). A supporting Python script (`scripts/self_improve.py`) provides git infrastructure CLI commands (clone, branch, test, commit, push/merge, cleanup, pull). A second script (`scripts/destructive_check.py`) detects destructive changes in the diff before any commit. All git work happens in a sibling `experiment/atelier/` clone — the production repo is never touched during a cycle.

**Tech Stack:** Python 3.11+, subprocess, shutil, re, json, sys.argv, pytest, git CLI

---

## File Structure

| File | Role |
|---|---|
| `skills/self-improve/SKILL.md` | Procedure: PM agenda → parallel analysis → meeting → implementation → gates |
| `scripts/destructive_check.py` | Library + CLI: detects destructive changes in a git diff |
| `scripts/self_improve.py` | Library + CLI: git operations (clone, branch, test, commit, push, merge, cleanup, pull) |
| `tests/test_destructive_check.py` | Tests for destructive_check.py |
| `tests/test_self_improve.py` | Tests for self_improve.py functions |

---

### Task 1: Create skills/self-improve/SKILL.md

**Files:**
- Create: `skills/self-improve/SKILL.md`

This is a procedure document — no TDD. Write the complete content and commit.

- [ ] **Step 1: Create the skill directory and write SKILL.md**

```bash
mkdir skills/self-improve
```

Write this complete content to `skills/self-improve/SKILL.md`:

````markdown
# dev:self-improve

Autonomous multi-agent improvement of Atelier's own code, skills, and structure. Uses an isolated git clone, a structured meeting with world-class expert agents, unanimous consensus, and a full test gate before merging.

## Hard gate

**User-initiated only.** No agent may call this skill from within any workflow or script. If you are an agent, do not invoke `dev:self-improve`.

## Invocation

```
dev:self-improve [--cycles N] [--subject "<area to improve>"]
```

- `--cycles N` — number of independent improvement cycles (default: 1). Each cycle is fully independent.
- `--subject` — optional focus area (e.g., `"improve QA skill procedure"`). If omitted, PM decides.

Cycles run sequentially. A failure in one cycle does not block the next.

## Procedure

### Phase 1 — Agenda setting (PM)

1. Record cycle start time (UTC).

2. Read the entire repository:
   - All `skills/*/SKILL.md` files
   - All `scripts/*.py` files
   - All `migrations/*.sql` files
   - All `tests/` files
   - `docs/`, `CHANGELOG.md`, `CLAUDE.md`

3. **If `--subject` is provided:** Focus analysis on that subject area.
   **If no subject:** Audit the full codebase and decide which area most needs improvement. Record your reasoning — it becomes the PM Assessment section.

4. Produce:
   - A numbered agenda (each item is a specific improvement question)
   - A list of agents to summon from the 61-role roster (select by domain relevance)

5. Write the minutes file header to `docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md`:

```markdown
# Self-Improvement Meeting — Cycle N
**Date:** YYYY-MM-DD HH:MM UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. [Name] | [Role] |

## PM Assessment *(only if no --subject provided)*
[Reasoning for chosen focus area]

## Agenda
1. [Improvement question]
2. ...
```

**Agents with standing relevance to every cycle:**
- Agent Systems Architect (Dr. Nadia Petrov) — agent orchestration and coordination
- AI Safety Researcher (Dr. Fatima Al-Rashid) — failure modes and alignment
- Prompt Engineer (Dr. Yusuf Okafor) — SKILL.md procedure quality
- AI Ethicist (Dr. Yewande Diallo) — bias and governance
- AI Research Scientist (Dr. Amara Osei-Bonsu) — theoretical soundness
- Cognitive Scientist (Dr. Aisha Mensah) — cognitive alignment of procedures

### Phase 2 — Parallel pre-analysis

6. Dispatch all summoned agents in parallel. Each independently reads the codebase areas relevant to their domain and writes a structured proposal:
   - What they found (specific files, patterns, problems)
   - What they propose to change and why
   - Risk classification: destructive or non-destructive

7. Collect all proposals before the meeting begins.

### Phase 3 — Synthesis meeting

8. PM facilitates a structured debate of each agenda item. For each item:
   - Present all proposals
   - Agents raise objections or support
   - Revise until unanimous agreement, or drop the item
   - Record the outcome in the minutes

9. Complete the minutes document:

```markdown
## Discussion

### Agenda Item 1: [Title]
**Proposals:**
- Dr. [Name] ([Role]): [Proposal summary]

**Discussion:** [Debate and resolution summary]

**Decision:** [Agreed change] — *Unanimous*
*or*
**Decision:** DROPPED — [reason no consensus reached]

## Decisions Log
1. [Decision text] — [file(s) affected]
2. ...

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | [what] | [where] | [agent] |
```

### Phase 4 — Implementation in experiment clone

10. Set up the isolated clone and feature branch:
```
python scripts/self_improve.py clone <cycle_n>
```
The command prints `CLONE_DIR=<path>` and `BRANCH=<name>`. Record both.

11. Write the completed minutes file into the clone at:
```
<clone_dir>/docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
```

12. Each assigned agent implements their action items by making changes directly in `<clone_dir>`.

13. Check for destructive changes:
```
python scripts/self_improve.py check-destructive <clone_dir>
```
- Exit 0: no destructive changes — proceed.
- Exit 1: review the JSON output. For each destructive change, ask the user:
  > "Cycle N proposes a destructive change: [description]. Approve? (y/n)"
  - Approved: proceed.
  - Rejected: revert that change in the clone, re-run check until exit 0.

### Phase 5 — Quality gates, push, cleanup

14. Run the full test suite in the clone:
```
python scripts/self_improve.py run-tests <clone_dir>
```
- **Pass (exit 0):** proceed to step 15.
- **Fail (exit 1):** ABORT. Append to minutes: `## Outcome\nFAILED — tests did not pass`. Go to step 17.

15. Commit all changes:
```
python scripts/self_improve.py commit <clone_dir> <cycle_n> "<subject>" "<d1>|<d2>" "<p1>|<p2>" <test_count> "docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md"
```

Where:
- `<subject>` — user-provided subject or the string `PM-directed`
- `<d1>|<d2>` — pipe-separated decisions from the log
- `<p1>|<p2>` — pipe-separated participant names
- `<test_count>` — number from `run-tests` output

16. Push and merge:
```
# If all changes are non-destructive (or all approved):
python scripts/self_improve.py push-merge <clone_dir> <branch>

# If any destructive change is awaiting approval (skip auto-merge):
python scripts/self_improve.py push-merge <clone_dir> <branch> skip
```

17. Clean up:
```
python scripts/self_improve.py cleanup
```

18. If auto-merged, pull main:
```
python scripts/self_improve.py pull
```

19. Print cycle summary:
```
Cycle N — [PASSED / FAILED / AWAITING APPROVAL]
Subject: [subject or PM-directed]
Participants: [N agents]
Decisions: [N agreed / M dropped]
Minutes: docs/self-improve/YYYY-MM-DD-cycle-N-minutes.md
Branch: self-improve/cycle-N-YYYY-MM-DD [merged / pending / not pushed]
```

## Hard rules
- User-initiated only. Abort if called from an agent context.
- Unanimous consent required. No item proceeds with a dissenting agent.
- Tests must pass before commit. Failure aborts the cycle — no exceptions.
- Destructive changes require explicit user approval before merging.
- `experiment/` is always deleted, whether the cycle passes, fails, or aborts.
- Every cycle produces a complete Markdown meeting minutes document.
- One commit per cycle — changes and minutes file together.
````

- [ ] **Step 2: Verify the file exists and is non-empty**

```bash
wc -l skills/self-improve/SKILL.md
```

Expected: 100+ lines.

- [ ] **Step 3: Commit**

```bash
git add skills/self-improve/SKILL.md
git commit -m "feat(skill): dev:self-improve — procedure document"
```

---

### Task 2: Create scripts/destructive_check.py (TDD)

**Files:**
- Create: `tests/test_destructive_check.py`
- Create: `scripts/destructive_check.py`

- [ ] **Step 1: Write the failing tests**

Write this complete content to `tests/test_destructive_check.py`:

```python
"""Tests for scripts/destructive_check.py"""
import textwrap
from pathlib import Path

import pytest

from scripts.destructive_check import (
    detect_destructive,
    _deleted_file_paths,
    _is_imported_by_any_file,
)


def _delete_diff(filepath: str, body: str = "pass") -> str:
    """Build a minimal unified diff that deletes filepath."""
    lines = body.splitlines()
    removed = "\n".join(f"-{line}" for line in lines)
    return textwrap.dedent(f"""\
        diff --git a/{filepath} b/{filepath}
        deleted file mode 100644
        index abc123..0000000
        --- a/{filepath}
        +++ /dev/null
        @@ -1,{len(lines)} +0,0 @@
        {removed}
    """)


# ── _deleted_file_paths ────────────────────────────────────────────────────

class TestDeletedFilePaths:
    def test_single_deleted_file(self):
        diff = _delete_diff("scripts/db.py", "def get_connection(): pass")
        assert _deleted_file_paths(diff) == ["scripts/db.py"]

    def test_no_deleted_files(self):
        diff = "+new line\n-old line\n"
        assert _deleted_file_paths(diff) == []

    def test_multiple_deleted_files(self):
        diff = _delete_diff("scripts/db.py") + "\n" + _delete_diff("scripts/old.py")
        paths = _deleted_file_paths(diff)
        assert "scripts/db.py" in paths
        assert "scripts/old.py" in paths


# ── _is_imported_by_any_file ───────────────────────────────────────────────

class TestIsImportedByAnyFile:
    def test_imported_via_from_import(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text(
            "from scripts.db import get_connection\n"
        )
        assert _is_imported_by_any_file("scripts/db.py", tmp_path) is True

    def test_not_imported(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("x = 1\n")
        assert _is_imported_by_any_file("scripts/db.py", tmp_path) is False

    def test_markdown_file_never_imported(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text(
            "from scripts.db import get_connection\n"
        )
        assert _is_imported_by_any_file("docs/readme.md", tmp_path) is False


# ── detect_destructive ─────────────────────────────────────────────────────

class TestDetectDestructive:
    def test_deleted_imported_file_flagged(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text(
            "from scripts.db import get_connection\n"
        )
        diff = _delete_diff("scripts/db.py", "def get_connection(): pass")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "deleted_imported_file" for i in issues)

    def test_deleted_non_imported_file_not_flagged(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("x = 1\n")
        diff = _delete_diff("docs/old_readme.md", "# old")
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "deleted_imported_file" for i in issues)

    def test_removed_public_function_flagged(self, tmp_path):
        diff = "-def get_phase(project_id):\n-    pass\n"
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_public_function" for i in issues)

    def test_removed_private_function_not_flagged(self, tmp_path):
        diff = "-def _internal_helper():\n-    pass\n"
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "removed_public_function" for i in issues)

    def test_db_drop_table_flagged(self, tmp_path):
        diff = "+DROP TABLE sessions;\n"
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "destructive_db_migration" for i in issues)

    def test_db_drop_column_flagged(self, tmp_path):
        diff = "+ALTER TABLE projects DROP COLUMN description;\n"
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "destructive_db_migration" for i in issues)

    def test_removed_skill_dir_flagged(self, tmp_path):
        diff = _delete_diff("skills/dev-qa/SKILL.md", "# dev:qa")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_skill_directory" for i in issues)

    def test_removed_test_file_flagged(self, tmp_path):
        diff = _delete_diff("tests/test_workflow.py", "def test_x(): pass")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_test_file" for i in issues)

    def test_no_destructive_changes_returns_empty(self, tmp_path):
        diff = "+new line added\n+another addition\n"
        issues = detect_destructive(diff, tmp_path)
        assert issues == []

    def test_returns_list_of_dicts_with_required_keys(self, tmp_path):
        diff = "+DROP TABLE sessions;\n"
        issues = detect_destructive(diff, tmp_path)
        for issue in issues:
            assert "type" in issue
            assert "description" in issue
            assert "file" in issue
```

- [ ] **Step 2: Run tests to confirm they all fail**

```bash
cd "C:/Users/user/Documents/Skills/atelier"
PYTHONPATH=. python -m pytest tests/test_destructive_check.py -v 2>&1 | head -20
```

Expected: `ImportError` — `scripts.destructive_check` not found.

- [ ] **Step 3: Write scripts/destructive_check.py**

Write this complete content to `scripts/destructive_check.py`:

```python
"""Detect destructive changes in a git diff for Atelier self-improvement cycles."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def get_diff(clone_dir: Path) -> str:
    """Return the full diff of all changes against HEAD in the clone."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout


def _deleted_file_paths(diff_text: str) -> list[str]:
    """Extract paths of deleted files from a git diff."""
    pattern = re.compile(
        r"^diff --git a/(.+?) b/\1\ndeleted file mode", re.MULTILINE
    )
    return [m.group(1) for m in pattern.finditer(diff_text)]


def _is_imported_by_any_file(filepath: str, repo_dir: Path) -> bool:
    """Return True if any Python file in repo_dir imports filepath."""
    if not filepath.endswith(".py"):
        return False
    module_parts = Path(filepath).with_suffix("").parts
    import_patterns = [
        f"from {'.'.join(module_parts)} import",
        f"import {'.'.join(module_parts)}",
        f"from {module_parts[-1]} import",
        f"import {module_parts[-1]}",
    ]
    for py_file in repo_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            if any(p in content for p in import_patterns):
                return True
        except OSError:
            continue
    return False


def _check_deleted_files(diff_text: str, repo_dir: Path) -> list[dict]:
    """Flag deleted Python files that are imported by other files."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if _is_imported_by_any_file(path, repo_dir):
            issues.append({
                "type": "deleted_imported_file",
                "description": f"Deleted '{path}' is imported by other files",
                "file": path,
            })
    return issues


def _check_removed_public_functions(diff_text: str) -> list[dict]:
    """Flag removed public function definitions (not starting with _)."""
    issues = []
    for m in re.finditer(r"^-(?:    )?def ([a-zA-Z][a-zA-Z0-9_]*)\(", diff_text, re.MULTILINE):
        issues.append({
            "type": "removed_public_function",
            "description": f"Public function '{m.group(1)}' was removed",
            "file": "unknown",
        })
    return issues


def _check_db_migrations(diff_text: str) -> list[dict]:
    """Flag SQL that drops or renames tables/columns."""
    issues = []
    pattern = re.compile(
        r"^\+.*(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+TABLE|RENAME\s+COLUMN"
        r"|ALTER\s+TABLE\s+\w+\s+RENAME)",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in pattern.finditer(diff_text):
        issues.append({
            "type": "destructive_db_migration",
            "description": f"Destructive SQL: {m.group(0).strip()}",
            "file": "migration",
        })
    return issues


def _check_removed_skill_dirs(diff_text: str) -> list[dict]:
    """Flag deleted SKILL.md files (skill directory removed)."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if path.startswith("skills/") and path.endswith("SKILL.md"):
            issues.append({
                "type": "removed_skill_directory",
                "description": f"Skill '{Path(path).parent.name}' was removed",
                "file": path,
            })
    return issues


def _check_removed_tests(diff_text: str) -> list[dict]:
    """Flag deleted test files."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if Path(path).name.startswith("test_") and path.endswith(".py"):
            issues.append({
                "type": "removed_test_file",
                "description": f"Test file '{path}' was deleted",
                "file": path,
            })
    return issues


def detect_destructive(diff_text: str, repo_dir: Path) -> list[dict]:
    """
    Scan a git diff for destructive changes.

    Returns a list of dicts with keys: type, description, file.
    Empty list means no destructive changes detected.
    """
    issues: list[dict] = []
    issues.extend(_check_deleted_files(diff_text, repo_dir))
    issues.extend(_check_removed_public_functions(diff_text))
    issues.extend(_check_db_migrations(diff_text))
    issues.extend(_check_removed_skill_dirs(diff_text))
    issues.extend(_check_removed_tests(diff_text))
    return issues


if __name__ == "__main__":
    # CLI: python scripts/destructive_check.py <clone_dir>
    if len(sys.argv) < 2:
        print("Usage: python scripts/destructive_check.py <clone_dir>")
        sys.exit(1)
    clone_dir = Path(sys.argv[1])
    diff = get_diff(clone_dir)
    issues = detect_destructive(diff, clone_dir)
    print(json.dumps(issues, indent=2))
    if issues:
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_destructive_check.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/destructive_check.py tests/test_destructive_check.py
git commit -m "feat: destructive_check.py — detects destructive git diff changes"
```

---

### Task 3: Create scripts/self_improve.py — git operation functions (TDD)

**Files:**
- Create: `tests/test_self_improve.py`
- Create: `scripts/self_improve.py`

- [ ] **Step 1: Write the failing tests**

Write this complete content to `tests/test_self_improve.py`:

```python
"""Tests for scripts/self_improve.py git operations."""
import subprocess
from pathlib import Path

import pytest


# ── Git fixtures ───────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, encoding="utf-8")


@pytest.fixture
def bare_remote(tmp_path):
    """Bare repo acting as origin."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return remote


@pytest.fixture
def source_repo(tmp_path, bare_remote):
    """Local repo with one passing test, pushed to bare_remote."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@test.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["remote", "add", "origin", str(bare_remote)], repo)
    (repo / "README.md").write_text("# Atelier\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_dummy.py").write_text("def test_ok(): assert True\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    _git(["push", "-u", "origin", "main"], repo)
    return repo


# ── clone_repo ────────────────────────────────────────────────────────────

class TestCloneRepo:
    def test_clone_creates_directory_with_contents(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        assert dest.exists()
        assert (dest / "README.md").exists()

    def test_clone_sets_git_identity(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "self-improve@atelier.local"


# ── create_branch ─────────────────────────────────────────────────────────

class TestCreateBranch:
    def test_branch_name_starts_with_prefix(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 3)
        assert branch.startswith("self-improve/cycle-3-")

    def test_branch_is_checked_out(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 1)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == branch


# ── run_tests_in_clone ────────────────────────────────────────────────────

class TestRunTestsInClone:
    def test_passing_tests_returns_true_and_count(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, run_tests_in_clone
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, count = run_tests_in_clone(dest)
        assert passed is True
        assert count == 1

    def test_failing_tests_returns_false(self, tmp_path, bare_remote, source_repo):
        # Push a failing test to the remote
        (source_repo / "tests" / "test_fail.py").write_text("def test_fail(): assert False\n")
        _git(["add", "."], source_repo)
        _git(["commit", "-m", "add failing test"], source_repo)
        _git(["push"], source_repo)
        from scripts.self_improve import clone_repo, run_tests_in_clone
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, _ = run_tests_in_clone(dest)
        assert passed is False


# ── write_minutes ─────────────────────────────────────────────────────────

class TestWriteMinutes:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        from scripts.self_improve import write_minutes
        path = tmp_path / "docs" / "self-improve" / "cycle-1-minutes.md"
        content = "# Meeting\n\n## Agenda\n1. Improve things"
        write_minutes(path, content)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content

    def test_overwrites_existing_file(self, tmp_path):
        from scripts.self_improve import write_minutes
        path = tmp_path / "minutes.md"
        path.write_text("old content")
        write_minutes(path, "new content")
        assert path.read_text(encoding="utf-8") == "new content"


# ── commit_cycle ──────────────────────────────────────────────────────────

class TestCommitCycle:
    def test_commit_message_contains_required_fields(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, commit_cycle
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        create_branch(dest, 2)
        (dest / "CHANGES.txt").write_text("a change")
        commit_cycle(
            clone_dir=dest,
            cycle_n=2,
            decisions=["Improve error handling in workflow.py", "Add retry logic"],
            participants=["Dr. Priya Nair", "Dr. Nadia Petrov"],
            n_tests=7,
            subject="improve error handling",
            minutes_rel_path="docs/self-improve/2026-05-14-cycle-2-minutes.md",
        )
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=dest, capture_output=True, text=True,
        )
        msg = result.stdout
        assert "self-improve(cycle-2):" in msg
        assert "Decisions:" in msg
        assert "1. Improve error handling in workflow.py" in msg
        assert "Tests: 7 passed" in msg
        assert "Subject: improve error handling" in msg
        assert "Dr. Priya Nair" in msg

    def test_commit_stages_all_changes(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, commit_cycle
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        create_branch(dest, 1)
        (dest / "new_file.txt").write_text("new")
        commit_cycle(
            clone_dir=dest, cycle_n=1,
            decisions=["Add file"], participants=["Dr. Test"],
            n_tests=1, subject="test",
            minutes_rel_path="docs/self-improve/minutes.md",
        )
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:"],
            cwd=dest, capture_output=True, text=True,
        )
        assert "new_file.txt" in result.stdout


# ── cleanup_experiment ────────────────────────────────────────────────────

class TestCleanupExperiment:
    def test_removes_directory_recursively(self, tmp_path):
        from scripts.self_improve import cleanup_experiment
        exp = tmp_path / "experiment"
        (exp / "atelier").mkdir(parents=True)
        (exp / "atelier" / "file.txt").write_text("x")
        cleanup_experiment(exp)
        assert not exp.exists()

    def test_no_error_if_already_absent(self, tmp_path):
        from scripts.self_improve import cleanup_experiment
        cleanup_experiment(tmp_path / "nonexistent")  # must not raise
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
PYTHONPATH=. python -m pytest tests/test_self_improve.py -v 2>&1 | head -15
```

Expected: `ImportError` — `scripts.self_improve` not found.

- [ ] **Step 3: Write scripts/self_improve.py (functions)**

Write this complete content to `scripts/self_improve.py`:

```python
"""Atelier self-improvement cycle — git infrastructure CLI."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Internal git helper ────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


# ── Public functions ───────────────────────────────────────────────────────

def get_remote_url(repo_dir: Path) -> str:
    """Return the origin remote URL of the production repo."""
    result = _git(["remote", "get-url", "origin"], repo_dir)
    return result.stdout.strip()


def clone_repo(remote_url: str, dest: Path) -> None:
    """Clone remote_url into dest and configure a known git identity."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", remote_url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(["config", "user.email", "self-improve@atelier.local"], dest)
    _git(["config", "user.name", "Atelier Self-Improve"], dest)


def create_branch(clone_dir: Path, cycle_n: int) -> str:
    """Create and checkout self-improve/cycle-N-YYYY-MM-DD. Returns branch name."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch = f"self-improve/cycle-{cycle_n}-{date_str}"
    _git(["checkout", "-b", branch], clone_dir)
    return branch


def run_tests_in_clone(clone_dir: Path) -> tuple[bool, int]:
    """Run pytest in clone_dir. Returns (all_passed, test_count)."""
    result = subprocess.run(
        ["python", "-m", "pytest", "-v", "--tb=short"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    count = 0
    for line in result.stdout.splitlines():
        m = re.search(r"(\d+) passed", line)
        if m:
            count = int(m.group(1))
    return result.returncode == 0, count


def write_minutes(minutes_path: Path, content: str) -> None:
    """Ensure parent dirs exist and write the meeting minutes Markdown file."""
    minutes_path.parent.mkdir(parents=True, exist_ok=True)
    minutes_path.write_text(content, encoding="utf-8")


def commit_cycle(
    clone_dir: Path,
    cycle_n: int,
    decisions: list[str],
    participants: list[str],
    n_tests: int,
    subject: str,
    minutes_rel_path: str,
) -> None:
    """Stage all changes and produce the standard self-improve commit."""
    _git(["add", "-A"], clone_dir)
    summary = decisions[0] if decisions else "improvements applied"
    decisions_text = "\n".join(f"  {i + 1}. {d}" for i, d in enumerate(decisions))
    msg = (
        f"self-improve(cycle-{cycle_n}): {summary}\n\n"
        f"Meeting: {minutes_rel_path}\n"
        f"Participants: {', '.join(participants)}\n"
        f"Decisions:\n{decisions_text}\n"
        f"Tests: {n_tests} passed\n"
        f"Subject: {subject}"
    )
    _git(["commit", "-m", msg], clone_dir)


def push_branch(clone_dir: Path, branch: str) -> None:
    """Push branch to origin from the clone."""
    _git(["push", "origin", branch], clone_dir)


def auto_merge_to_main(repo_dir: Path, branch: str) -> None:
    """Merge branch into main in the production repo and push."""
    _git(["checkout", "main"], repo_dir)
    _git(["pull", "origin", "main"], repo_dir)
    _git(["merge", "--no-ff", branch, "-m", f"Merge {branch} into main"], repo_dir)
    _git(["push", "origin", "main"], repo_dir)


def cleanup_experiment(experiment_dir: Path) -> None:
    """Delete the experiment directory. Safe if it does not exist."""
    if experiment_dir.exists():
        shutil.rmtree(experiment_dir)


def pull_main(repo_dir: Path) -> None:
    """Pull main in the production repo."""
    _git(["pull", "origin", "main"], repo_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_self_improve.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
PYTHONPATH=. python -m pytest -v 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/self_improve.py tests/test_self_improve.py
git commit -m "feat: self_improve.py — git operation functions (clone, branch, test, commit, push, cleanup)"
```

---

### Task 4: Add CLI to scripts/self_improve.py (TDD)

**Files:**
- Modify: `scripts/self_improve.py` (append `__main__` block)
- Modify: `tests/test_self_improve.py` (append CLI tests)

The CLI follows the same `sys.argv` pattern as `workflow.py` and `session.py`.

- [ ] **Step 1: Write the failing CLI tests**

Append this class to the end of `tests/test_self_improve.py`:

```python
# ── CLI ───────────────────────────────────────────────────────────────────

class TestCLI:
    def test_unknown_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/self_improve.py", "bogus"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_no_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/self_improve.py"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_run_tests_pass_exits_0(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "run-tests", str(dest)],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert "TESTS_PASSED=" in result.stdout

    def test_cleanup_exits_0(self, tmp_path):
        exp = tmp_path / "experiment"
        exp.mkdir()
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "cleanup", str(exp)],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert not exp.exists()
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
PYTHONPATH=. python -m pytest tests/test_self_improve.py::TestCLI -v 2>&1 | head -20
```

Expected: failures — CLI not implemented yet.

- [ ] **Step 3: Append the __main__ block to scripts/self_improve.py**

Append this complete block to the end of `scripts/self_improve.py`:

```python

if __name__ == "__main__":
    # Usage patterns:
    #   python scripts/self_improve.py clone <cycle_n>
    #   python scripts/self_improve.py check-destructive <clone_dir>
    #   python scripts/self_improve.py run-tests <clone_dir>
    #   python scripts/self_improve.py commit <clone_dir> <cycle_n> <subject> <decisions> <participants> <n_tests> <minutes_path>
    #   python scripts/self_improve.py push-merge <clone_dir> <branch> [skip]
    #   python scripts/self_improve.py cleanup [<experiment_dir>]
    #   python scripts/self_improve.py pull

    from scripts.destructive_check import get_diff, detect_destructive

    repo_dir = Path.cwd()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "clone":
        cycle_n = int(sys.argv[2])
        remote_url = get_remote_url(repo_dir)
        experiment_dir = repo_dir.parent / "experiment"
        clone = experiment_dir / repo_dir.name
        clone_repo(remote_url, clone)
        branch = create_branch(clone, cycle_n)
        print(f"CLONE_DIR={clone}")
        print(f"BRANCH={branch}")

    elif cmd == "check-destructive":
        clone = Path(sys.argv[2])
        diff = get_diff(clone)
        issues = detect_destructive(diff, clone)
        print(json.dumps(issues, indent=2))
        if issues:
            sys.exit(1)

    elif cmd == "run-tests":
        clone = Path(sys.argv[2])
        passed, count = run_tests_in_clone(clone)
        print(f"TESTS_PASSED={count}")
        if not passed:
            sys.exit(1)

    elif cmd == "commit":
        # commit <clone_dir> <cycle_n> <subject> "<d1>|<d2>" "<p1>|<p2>" <n_tests> <minutes_path>
        clone = Path(sys.argv[2])
        cycle_n = int(sys.argv[3])
        subject = sys.argv[4]
        decisions = [d for d in sys.argv[5].split("|") if d.strip()]
        participants = [p for p in sys.argv[6].split("|") if p.strip()]
        n_tests = int(sys.argv[7])
        minutes_rel = sys.argv[8]
        commit_cycle(clone, cycle_n, decisions, participants, n_tests, subject, minutes_rel)
        print("Committed.")

    elif cmd == "push-merge":
        clone = Path(sys.argv[2])
        branch = sys.argv[3]
        skip_merge = len(sys.argv) > 4 and sys.argv[4] == "skip"
        push_branch(clone, branch)
        if not skip_merge:
            auto_merge_to_main(repo_dir, branch)
            pull_main(repo_dir)
            print(f"Merged {branch} into main and pulled.")
        else:
            print(f"Branch {branch} pushed. Awaiting human approval to merge.")

    elif cmd == "cleanup":
        exp = Path(sys.argv[2]) if len(sys.argv) > 2 else repo_dir.parent / "experiment"
        cleanup_experiment(exp)
        print("experiment/ removed.")

    elif cmd == "pull":
        pull_main(repo_dir)
        print("Pulled main.")

    else:
        print(
            "Commands: clone, check-destructive, run-tests, commit, "
            "push-merge, cleanup, pull"
        )
        sys.exit(1)
```

- [ ] **Step 4: Run CLI tests to verify they pass**

```bash
PYTHONPATH=. python -m pytest tests/test_self_improve.py::TestCLI -v
```

Expected: all 4 CLI tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
PYTHONPATH=. python -m pytest -v 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/self_improve.py tests/test_self_improve.py
git commit -m "feat: self_improve.py — add CLI subcommands (clone, check-destructive, run-tests, commit, push-merge, cleanup, pull)"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| User-only invocation | SKILL.md hard rule + Hard rules section |
| PM agenda setting (with/without subject) | SKILL.md Phase 1 |
| 61-role roster, domain selection, standing AI experts | SKILL.md Phase 1 |
| Parallel pre-analysis | SKILL.md Phase 2 |
| Synthesis meeting, unanimous consensus, minutes format | SKILL.md Phase 3 |
| Experiment clone setup, sibling directory | Task 3 `clone_repo` + Task 4 `clone` CLI |
| Feature branch naming | Task 3 `create_branch` |
| Destructive change detection (5 categories) | Task 2 `destructive_check.py` |
| User approval for destructive changes | SKILL.md Phase 4 step 13 |
| Full test gate | Task 3 `run_tests_in_clone` + Task 4 `run-tests` CLI |
| Standard commit format (decisions, participants, tests, subject, minutes) | Task 3 `commit_cycle` + Task 4 `commit` CLI |
| Push + auto-merge to main | Task 3 `push_branch` + `auto_merge_to_main` |
| experiment/ cleanup always runs | Task 3 `cleanup_experiment` + SKILL.md step 17 |
| Pull after auto-merge | Task 3 `pull_main` + SKILL.md step 18 |
| Markdown meeting minutes written per cycle | SKILL.md Phase 3 + `write_minutes` |
| Cycle summary printed | SKILL.md step 19 |
| Independent cycles, sequential execution | SKILL.md invocation section |

**Placeholder scan:** No TBD, TODO, or incomplete steps. All code blocks are complete.

**Type consistency:**
- `clone_repo(remote_url: str, dest: Path)` — used in Task 3 tests and Task 4 CLI. ✅
- `create_branch(clone_dir: Path, cycle_n: int) -> str` — consistent throughout. ✅
- `run_tests_in_clone(clone_dir: Path) -> tuple[bool, int]` — consistent. ✅
- `commit_cycle(clone_dir, cycle_n, decisions, participants, n_tests, subject, minutes_rel_path)` — all args used correctly in tests and CLI. ✅
- `cleanup_experiment(experiment_dir: Path)` — consistent. ✅
