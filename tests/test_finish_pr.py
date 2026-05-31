"""pytest suite for `scripts/finish_pr.py` — agent-team-mode dev-finish (#66 / S1).

`scripts.finish_pr` is the thin, testable git/gh layer behind the dev-finish
team-mode sub-procedure (AC1/AC2/AC7). Three surfaces:

  * ``merge_worktrees`` — merge N task branches into the per-run feature branch
    ``atelier/<slug>`` (NOT into base) in dependency order, ``--no-ff`` with a
    clean conflict-abort; clean worktrees auto-removed, DIRTY ones PRESERVED
    and listed for the PR body. Dirtiness reuses ``worktree.classify_status``'s
    ``?? .claude/``-aware split so a worktree dirty ONLY with harness storage
    counts CLEAN.
  * ``resolve_or_create_feature_branch`` — read the canonical ``atelier/<slug>``
    branch back via ``git branch --list`` (F4-analog: never re-derive the
    slug); create off base only when absent.
  * ``open_pr`` — single ``gh pr create --base --head`` against the feature
    branch; the gh/subprocess call is injectable so tests never shell out.
  * ``write_retrospective`` — ONE ``backend.write_document(domain='project_doc',
    subdomain='finish-result', ...)`` via the A2 facade — mode-symmetric (no
    SKILL-level or finish_pr-level mode branch).

The Iron-Law non-vacuity guard (``test_retrospective_write_is_non_vacuous``)
FAILS if the ``write_document`` call is removed (call count drops to 0) or its
domain/subdomain drift — mirroring
``test_abort.py::test_doc_persistence_is_non_vacuous``.

Git-op tests stand up a REAL throw-away git repo with REAL linked worktrees in
``tmp_path`` (git ops are mode-agnostic). The retrospective-write test runs in
BOTH modes: Local against a real ``.ai/atelier.db`` (assert the row lands) and
forced-Memex via the canonical hermetic stub set (assert at the
``backend_memex.write_document`` mock boundary, ``subdomain`` folded into the
adapted metadata).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import backend, finish_pr, mode_detector
from scripts.git_utils import git as _git
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

SLUG = "demo-repo"
FEATURE_BRANCH = f"atelier/{SLUG}"


# Task branches live in a SIBLING namespace, not nested UNDER the feature
# branch. git's loose-ref storage forbids both `refs/heads/atelier/<slug>`
# (a file) and `refs/heads/atelier/<slug>/<task>` (a directory) — so the
# task-branch scheme is `atelier/<slug>-task-<id>`, which a correct dispatch
# layer must use. `merge_worktrees` itself is naming-agnostic (it merges
# whatever branch list it is handed), so this constraint lives at the caller.
def _task_branch(task_id: str) -> str:
    return f"{FEATURE_BRANCH}-task-{task_id}"


# ── Real-git repo fixture (mode-agnostic git ops) ───────────────────────────


def _commit_file(repo: Path, name: str, content: str, msg: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", msg], repo)


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with a ``base`` branch and one initial commit.

    Returns ``(repo_root, base_branch)``. Configures a deterministic identity
    so commits succeed in a clean CI sandbox.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "base"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    _commit_file(repo, "README.md", "base\n", "initial commit on base")
    return repo, "base"


def _make_task_worktree(repo: Path, base: str, task_id: str, files: dict[str, str]) -> Path:
    """Create a linked worktree on the sibling-namespace task branch off
    ``base`` and commit ``files`` into it. Returns the worktree path."""
    branch = _task_branch(task_id)
    wt_dir = repo.parent / f"wt-{task_id}"
    _git(["worktree", "add", "-b", branch, str(wt_dir), base], repo)
    _git(["config", "user.email", "t@example.com"], wt_dir)
    _git(["config", "user.name", "Test"], wt_dir)
    for name, content in files.items():
        (wt_dir / name).write_text(content, encoding="utf-8")
    _git(["add", "-A"], wt_dir)
    _git(["commit", "-m", f"task {task_id} work"], wt_dir)
    return wt_dir


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git(["branch", "--list", branch], repo).stdout.strip()
    return bool(out)


def _file_on_branch(repo: Path, branch: str, name: str) -> bool:
    res = _git(["cat-file", "-e", f"{branch}:{name}"], repo, check=False)
    return res.returncode == 0


# ── resolve_or_create_feature_branch (F4-analog read-back) ──────────────────


def test_resolve_feature_branch_reads_existing_back(git_repo):
    """When ``atelier/<slug>`` already exists it is read back verbatim and NOT
    re-created (no new commits / no branch churn)."""
    repo, base = git_repo
    _git(["branch", FEATURE_BRANCH, base], repo)
    rev_before = _git(["rev-parse", FEATURE_BRANCH], repo).stdout.strip()

    branch = finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)

    assert branch == FEATURE_BRANCH
    assert _git(["rev-parse", FEATURE_BRANCH], repo).stdout.strip() == rev_before


def test_resolve_feature_branch_creates_when_absent(git_repo):
    """When absent the branch is created off ``base`` pointing at base's tip."""
    repo, base = git_repo
    assert not _branch_exists(repo, FEATURE_BRANCH)

    branch = finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)

    assert branch == FEATURE_BRANCH
    assert _branch_exists(repo, FEATURE_BRANCH)
    assert (
        _git(["rev-parse", FEATURE_BRANCH], repo).stdout.strip()
        == _git(["rev-parse", base], repo).stdout.strip()
    )


# ── merge_worktrees ─────────────────────────────────────────────────────────


def test_merge_worktrees_merges_into_feature_not_base(git_repo):
    """N task branches merge into ``atelier/<slug>`` in dep order; base is
    NEVER touched (critical risk: must NOT merge into base)."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    wt_a = _make_task_worktree(repo, base, "t1", {"a.txt": "a\n"})
    wt_b = _make_task_worktree(repo, base, "t2", {"b.txt": "b\n"})
    base_tip_before = _git(["rev-parse", base], repo).stdout.strip()

    result = finish_pr.merge_worktrees(
        repo,
        FEATURE_BRANCH,
        base,
        [_task_branch("t1"), _task_branch("t2")],
    )

    # Both task contributions reached the FEATURE branch...
    assert _file_on_branch(repo, FEATURE_BRANCH, "a.txt")
    assert _file_on_branch(repo, FEATURE_BRANCH, "b.txt")
    # ...and NOT the base branch.
    assert not _file_on_branch(repo, base, "a.txt")
    assert not _file_on_branch(repo, base, "b.txt")
    assert _git(["rev-parse", base], repo).stdout.strip() == base_tip_before
    # Dep order preserved in the merged list; nothing preserved; no conflicts.
    assert result.merged == [_task_branch("t1"), _task_branch("t2")]
    assert result.conflicts == []
    assert result.dirty_preserved == []
    # Worktrees with no dirty project files are auto-removed.
    assert not wt_a.exists()
    assert not wt_b.exists()


def test_merge_worktrees_uses_no_ff(git_repo):
    """Merges are ``--no-ff`` so each task lands as an explicit merge commit
    (two parents) even when a fast-forward was possible."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    _make_task_worktree(repo, base, "t1", {"a.txt": "a\n"})

    finish_pr.merge_worktrees(repo, FEATURE_BRANCH, base, [_task_branch("t1")])

    parents = _git(["rev-list", "--parents", "-n", "1", FEATURE_BRANCH], repo).stdout.split()
    # commit + 2 parents == --no-ff merge commit.
    assert len(parents) == 3, f"expected a no-ff merge commit (2 parents), got {parents}"


def test_merge_worktrees_preserves_dirty_worktree(git_repo):
    """A worktree with uncommitted PROJECT changes is PRESERVED (not removed)
    and reported with its porcelain status for the PR body."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    wt = _make_task_worktree(repo, base, "t1", {"a.txt": "a\n"})
    # Leave an uncommitted (tracked-file) change in the worktree.
    (wt / "a.txt").write_text("dirty edit\n", encoding="utf-8")

    result = finish_pr.merge_worktrees(repo, FEATURE_BRANCH, base, [_task_branch("t1")])

    assert result.merged == [_task_branch("t1")]
    assert wt.exists(), "dirty worktree must be PRESERVED, not removed"
    preserved_paths = [p for p, _status in result.dirty_preserved]
    assert str(wt) in preserved_paths or str(wt.resolve()) in preserved_paths
    # Status string is captured (non-empty) for the PR body.
    statuses = dict(result.dirty_preserved)
    captured = statuses.get(str(wt)) or statuses.get(str(wt.resolve()))
    assert captured and "a.txt" in captured


def test_merge_worktrees_claude_only_dirt_counts_clean(git_repo):
    """A worktree dirty ONLY with ``.claude/`` harness storage is CLEAN (the
    classify_status '?? .claude/' split is reused) — auto-removed, NOT
    preserved. Anti-revert: a naive ``git status`` dirtiness test would
    falsely preserve it."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    wt = _make_task_worktree(repo, base, "t1", {"a.txt": "a\n"})
    # Only untracked .claude/ harness storage — NOT a project change.
    (wt / ".claude").mkdir()
    (wt / ".claude" / "state.json").write_text("{}", encoding="utf-8")

    result = finish_pr.merge_worktrees(repo, FEATURE_BRANCH, base, [_task_branch("t1")])

    assert result.dirty_preserved == [], (
        ".claude/-only dirt must count CLEAN — the worktree should be auto-removed"
    )
    assert not wt.exists()


def test_merge_worktrees_conflict_aborts_cleanly(git_repo):
    """A merge conflict triggers ``git merge --abort``, is recorded in
    ``conflicts``, and leaves the feature branch + worktrees intact (no
    half-merged state)."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    # Both tasks touch the SAME file with divergent content off base.
    _make_task_worktree(repo, base, "t1", {"clash.txt": "from-t1\n"})
    wt_b = _make_task_worktree(repo, base, "t2", {"clash.txt": "from-t2\n"})
    feature_tip_before = _git(["rev-parse", FEATURE_BRANCH], repo).stdout.strip()

    result = finish_pr.merge_worktrees(
        repo,
        FEATURE_BRANCH,
        base,
        [_task_branch("t1"), _task_branch("t2")],
    )

    # First merges cleanly; second conflicts and is aborted.
    assert _task_branch("t1") in result.merged
    assert _task_branch("t2") in result.conflicts
    # No MERGE_HEAD left dangling — the abort was clean.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    # Feature branch advanced by exactly the clean merge, not the conflict.
    assert _git(["rev-parse", FEATURE_BRANCH], repo).stdout.strip() != feature_tip_before
    assert _file_on_branch(repo, FEATURE_BRANCH, "clash.txt")
    # The conflicting worktree is intact (never removed on conflict).
    assert wt_b.exists()
    # The cleanly-merged t1's contribution is on the feature branch.
    assert _file_on_branch(repo, FEATURE_BRANCH, "clash.txt")


# ── open_pr (injectable subprocess) ─────────────────────────────────────────


def test_open_pr_runs_gh_create_and_returns_url():
    """open_pr pushes the feature branch then runs a single
    ``gh pr create --base --head`` and returns the URL. The runner is
    injectable so no real gh/network call happens."""
    calls = []

    def fake_runner(cmd, cwd):
        calls.append((list(cmd), str(cwd)))
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://github.com/o/r/pull/42\n", stderr=""
        )

    url = finish_pr.open_pr(
        Path("/tmp/repo"),
        FEATURE_BRANCH,
        "main",
        "My PR",
        "body text",
        runner=fake_runner,
    )

    assert url == "https://github.com/o/r/pull/42"
    gh_calls = [c for c, _ in calls if c[0] == "gh"]
    assert len(gh_calls) == 1, "exactly one gh pr create invocation"
    gh = gh_calls[0]
    assert gh[:3] == ["gh", "pr", "create"]
    assert "--base" in gh and gh[gh.index("--base") + 1] == "main"
    assert "--head" in gh and gh[gh.index("--head") + 1] == FEATURE_BRANCH


def test_open_pr_raises_on_nonzero_gh():
    def fake_runner(cmd, cwd):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="gh pr create"):
        finish_pr.open_pr(Path("/tmp/repo"), FEATURE_BRANCH, "main", "t", "b", runner=fake_runner)


def test_open_pr_raises_on_empty_url():
    def fake_runner(cmd, cwd):
        return subprocess.CompletedProcess(cmd, 0, stdout="   \n", stderr="")

    with pytest.raises(RuntimeError, match="no URL"):
        finish_pr.open_pr(Path("/tmp/repo"), FEATURE_BRANCH, "main", "t", "b", runner=fake_runner)


# ── write_retrospective: Iron-Law non-vacuity ──────────────────────────────


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace (mirrors
    tests/test_abort.py::workspace). ``backend_local._conn()`` resolves via the
    CWD git root."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    return {"root": root, "db": str(db)}


def test_retrospective_write_is_non_vacuous(local_db, monkeypatch):
    """IRON-LAW non-vacuity guard: ``write_retrospective`` calls
    ``backend.write_document`` EXACTLY ONCE with domain='project_doc',
    subdomain='finish-result'. FAILS-RED if the write is removed (count 0) or
    the domain/subdomain drift — mirrors
    test_abort.py::test_doc_persistence_is_non_vacuous.

    Patches the facade to a COUNTING SPY that still delegates to the real
    write (so the row genuinely lands)."""
    calls = []
    real_write_document = backend.write_document

    def _spy(**kwargs):
        calls.append(kwargs)
        return real_write_document(**kwargs)

    monkeypatch.setattr(finish_pr.backend, "write_document", _spy)

    finish_pr.write_retrospective(
        workspace_id=None,
        project_id=None,
        title="Finish: demo-repo",
        body="merged 2 tasks; opened PR.",
        pr_url="https://github.com/o/r/pull/42",
    )

    retro_writes = [
        c
        for c in calls
        if c.get("domain") == "project_doc" and c.get("subdomain") == "finish-result"
    ]
    assert len(retro_writes) == 1, (
        "write_retrospective must call backend.write_document EXACTLY once with "
        "domain='project_doc', subdomain='finish-result' — the retro write was "
        f"removed or mis-routed (got {len(retro_writes)})"
    )
    # The metadata carries the phase + pr_url for the durable artifact.
    md = retro_writes[0]["metadata"]
    assert md.get("phase") == "finish"
    assert md.get("pr_url") == "https://github.com/o/r/pull/42"


def test_retrospective_write_lands_in_local_db(local_db):
    """Local mode: the retro doc genuinely lands (the spy-free row readback)."""
    finish_pr.write_retrospective(
        workspace_id=None,
        project_id=None,
        title="Finish: demo-repo",
        body="merged 2 tasks; opened PR.",
        pr_url="https://github.com/o/r/pull/42",
    )

    found = backend.find_documents(query="", domain="project_doc", subdomain="finish-result")
    assert len(found) >= 1
    doc = found[0]
    assert doc["domain"] == "project_doc"
    assert doc["subdomain"] == "finish-result"
    md = json.loads(doc["metadata"]) if doc.get("metadata") else {}
    assert md.get("pr_url") == "https://github.com/o/r/pull/42"


def test_retrospective_write_routes_through_memex_facade(tmp_path, monkeypatch):
    """Memex mode (AC2 mode-symmetry, NO mode branch in finish_pr): the single
    facade call routes to the Memex backend with subdomain='finish-result'
    folded into the adapted metadata blob. Drives the REAL facade; only the
    backend_memex leaf is stubbed (canonical hermetic stub set)."""
    from scripts import backend_memex

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    captured: dict = {}

    def _spy_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "idx-x"}

    monkeypatch.setattr(backend_memex, "write_document", _spy_write_document)

    finish_pr.write_retrospective(
        workspace_id=None,
        project_id=None,
        title="Finish: demo-repo",
        body="merged 2 tasks; opened PR.",
        pr_url="https://github.com/o/r/pull/42",
    )

    assert captured, "the Memex backend write_document leaf was never reached"
    assert captured["domain"] == "project_doc"
    md = captured["metadata"]
    # subdomain is folded into the metadata blob in Memex mode (facade adapter).
    assert md.get("subdomain") == "finish-result"
    assert md.get("pr_url") == "https://github.com/o/r/pull/42"
    assert md.get("phase") == "finish"


# ── Domain-vocabulary catalog extension (S1 one-liner) ──────────────────────


def test_project_doc_subdomain_catalog_includes_finish_result():
    """SUBDOMAINS['project_doc'] catalogs 'finish-result' so a future strict
    subdomain enforcement accepts the team-mode finish convention."""
    from scripts import domain_vocabulary as dv

    assert "finish-result" in dv.SUBDOMAINS["project_doc"], (
        "SUBDOMAINS['project_doc'] must catalog 'finish-result'"
    )


# ── #66 N0: merge-boundary branch-name guard (option-injection + sibling scheme) ─


@pytest.mark.parametrize(
    "bad_branch",
    [
        "-X",  # bare git option
        "--output=/tmp/pwn",  # long option with payload
        "-atelier/demo-repo",  # leading dash, otherwise plausible
        "main",  # not in the atelier/ namespace
        "atelier/demo-repo/t1",  # NESTED (collides) — not the sibling scheme
        "atelier/-leadingdash",  # leading-dash slug
    ],
)
def test_merge_worktrees_rejects_non_conforming_branch(git_repo, bad_branch):
    """#66 N0 SECURITY: a task branch that does not conform to the sibling
    namespace `atelier/<slug>[-task-<id>]` — including any leading-dash name git
    could parse as an OPTION — is rejected at the merge boundary with a clear
    ValueError BEFORE any checkout/merge runs. ANTI-REVERT: dropping the guard
    (or the `--` separators) re-opens git option-injection on the positionals."""
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)

    with pytest.raises(ValueError, match="sibling-namespace contract"):
        finish_pr.merge_worktrees(repo, FEATURE_BRANCH, base, [bad_branch])


def test_merge_worktrees_rejects_leading_dash_feature_branch(git_repo):
    """#66 N0: the FEATURE branch is guarded too — a leading-dash feature branch
    is rejected before any git op (it could be parsed as an option)."""
    repo, base = git_repo
    with pytest.raises(ValueError, match="sibling-namespace contract"):
        finish_pr.merge_worktrees(repo, "-feature", base, [])


def test_resolve_feature_branch_rejects_invalid_slug(git_repo):
    """#66 N0: resolve_or_create_feature_branch validates the derived feature
    branch — a slug producing a leading-dash / non-conforming name is rejected
    rather than silently created."""
    repo, base = git_repo
    with pytest.raises(ValueError, match="sibling-namespace contract"):
        finish_pr.resolve_or_create_feature_branch(repo, "-bad", base)


def test_open_pr_rejects_invalid_feature_branch():
    """#66 N0: open_pr guards the feature branch at the push/PR boundary so a
    malformed branch never reaches `git push` / `gh pr create` as a positional."""

    def fake_runner(cmd, cwd):  # pragma: no cover — must never be reached
        raise AssertionError("runner must not be called for an invalid branch")

    with pytest.raises(ValueError, match="sibling-namespace contract"):
        finish_pr.open_pr(Path("/tmp/repo"), "--head=evil", "main", "t", "b", runner=fake_runner)


# ── #66 N2: task_branch_name helper (sibling, non-colliding) ─────────────────


def test_task_branch_name_is_sibling_not_nested():
    """#66 N2: task_branch_name yields the SIBLING name
    `atelier/<slug>-task-<id>`, NOT the colliding nested `atelier/<slug>/<id>`."""
    name = finish_pr.task_branch_name(SLUG, "t1")
    assert name == f"atelier/{SLUG}-task-t1"
    # It is a sibling of the feature branch, NOT nested under it.
    assert name != f"{FEATURE_BRANCH}/t1"
    assert not name.startswith(f"{FEATURE_BRANCH}/")


def test_task_branch_name_does_not_collide_with_feature_branch(git_repo):
    """#66 N2: `git branch <feature>` then `git branch <task_branch_name>` BOTH
    succeed in a real repo — proving the task branch is a non-colliding sibling
    (a nested `atelier/<slug>/<id>` would fail with git's loose-ref D/F conflict
    once `refs/heads/atelier/<slug>` exists as a FILE)."""
    repo, base = git_repo
    feature = FEATURE_BRANCH
    task = finish_pr.task_branch_name(SLUG, "t1")

    # Both create cleanly (no directory/file ref conflict).
    _git(["branch", feature, base], repo)
    _git(["branch", task, base], repo)

    assert _branch_exists(repo, feature)
    assert _branch_exists(repo, task)
    # And the helper's name is what passes the merge-boundary guard.
    assert finish_pr._BRANCH_RE.match(task)


def test_nested_task_branch_collides_proving_sibling_is_required(git_repo):
    """#66 N2 (anti-naivety): the NESTED scheme `atelier/<slug>/<id>` genuinely
    collides — once the feature branch exists as a ref FILE, creating a nested
    branch fails with git's directory/file ref conflict. This pins WHY
    task_branch_name uses the sibling `-task-` scheme rather than nesting."""
    repo, base = git_repo
    _git(["branch", FEATURE_BRANCH, base], repo)
    nested = f"{FEATURE_BRANCH}/t1"

    res = _git(["branch", nested, base], repo, check=False)
    assert res.returncode != 0, (
        "a nested atelier/<slug>/<id> branch must FAIL once atelier/<slug> exists "
        "(loose-ref D/F conflict) — this is why the sibling -task- scheme is required"
    )


# ── #66 N4: feature-branch checkout failure still restores the original branch ─


def test_merge_worktrees_restores_original_branch_on_checkout_failure(git_repo, monkeypatch):
    """#66 N4 ROBUSTNESS: if the feature-branch checkout (now INSIDE the try)
    fails, the finally MUST still restore the ORIGINAL branch. We capture the
    original branch first, then make ONLY the feature-branch checkout raise; the
    restore checkout to the original branch must still be issued.

    ANTI-REVERT: with the pre-#66 ordering (feature checkout BEFORE the
    try/finally) the failure would skip the restore entirely and this goes RED.
    """
    repo, base = git_repo
    finish_pr.resolve_or_create_feature_branch(repo, SLUG, base)
    # Main worktree starts on `base` (the fixture's initial branch).
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() == base

    real_git = finish_pr._git
    restore_calls: list[list[str]] = []

    def flaky_git(args, cwd, check=True, **kwargs):
        # Sabotage ONLY the switch ONTO the feature branch.
        if args[:1] == ["checkout"] and len(args) >= 2 and args[1] == FEATURE_BRANCH:
            raise subprocess.CalledProcessError(1, ["git", *args])
        # Record the restore checkout (back onto the original branch).
        if args[:1] == ["checkout"] and len(args) >= 2 and args[1] == base:
            restore_calls.append(list(args))
        return real_git(args, cwd, check=check, **kwargs)

    monkeypatch.setattr(finish_pr, "_git", flaky_git)

    # The feature-branch checkout failure propagates (it is the caller's signal
    # the merge could not even start) — but the finally must STILL restore.
    with pytest.raises(
        subprocess.CalledProcessError, match=r"git', 'checkout', 'atelier/demo-repo"
    ):
        finish_pr.merge_worktrees(repo, FEATURE_BRANCH, base, [])

    # The finally restored (attempted to restore) the ORIGINAL branch despite the
    # feature-branch checkout having failed.
    assert restore_calls == [["checkout", base]], (
        "the original branch must be restored even when the feature-branch "
        f"checkout fails (N4); got restore calls: {restore_calls}"
    )
    # And the working tree is genuinely back on the original branch.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() == base
