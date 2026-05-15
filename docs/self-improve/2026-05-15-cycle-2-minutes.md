# Self-Improvement Meeting — Cycle 2
**Date:** 2026-05-15 10:00 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Yewande Diallo | AI Ethicist |
| Dr. Amara Osei-Bonsu | AI Research Scientist |
| Dr. Aisha Mensah | Cognitive Scientist |

## Agenda
1. Fix fragile stash detection in `auto_merge_to_main`
2. Eliminate `_git()` duplication across `self_improve.py` and `worktree.py`
3. Fix CRLF line ending bug in `parse_main_worktree` on Windows
4. Fix `run_tests_in_clone` crash on non-UTF-8 pytest output (Windows)

---

## Discussion

### Agenda Item 1: Stash detection fragility in `auto_merge_to_main`

**Proposals:**
- Dr. Petrov (Systems Architect): `stash_result.stdout.startswith("Saved")` is the correct idiom — git outputs `Saved working directory and index state ...` on success and `No local changes to save` when nothing was stashed. The current check `"No local changes" not in stdout` is an inverted negative that breaks on non-English git installations and on edge-case messages.
- Dr. Al-Rashid (Safety): The current logic also fails silently if `returncode != 0` for reasons other than "nothing to stash." Using `startswith("Saved")` is both explicit and locale-dependent; propose additionally checking `returncode == 0` as a conjunction.
- Dr. Osei-Bonsu (Research): `git stash push` exits 0 whether it stashed or not. The stdout signal is the correct discriminator. Recommend `stash_result.stdout.strip().startswith("Saved working directory")` for precision.

**Discussion:** All agents agreed that the inverted-negative check is fragile. The positive `startswith("Saved working directory")` is unambiguous. The `returncode == 0` conjunction is already in the current code and should be retained. Unanimous.

**Decision:** Replace `"No local changes" not in stash_result.stdout` with `stash_result.stdout.strip().startswith("Saved working directory")` — `scripts/self_improve.py` — *Unanimous*

---

### Agenda Item 2: `_git()` duplication

**Proposals:**
- Dr. Petrov (Systems Architect): Extract the `_git()` helper to `scripts/git_utils.py`. Both `self_improve.py` and `worktree.py` import it. Add an `errors` keyword argument (default `"strict"`) to allow callers to opt into `errors="replace"` for subprocesses that may emit non-UTF-8 output.
- Dr. Okafor (Prompt Engineer): Keep the helper in `git_utils.py` minimal — just the subprocess call. Do not add logic there. Callers control behavior via kwargs.
- Dr. Mensah (Cognitive Scientist): The duplication currently makes the two modules feel independent. Extraction is worth the coupling because the semantics are truly identical.

**Discussion:** Unanimous agreement to extract. The `errors` parameter should default to `"strict"` so existing callers are unaffected, with `run_tests_in_clone` using `errors="replace"` independently via `subprocess.run` (it already bypasses `_git`).

**Decision:** Create `scripts/git_utils.py` with `git()` helper accepting `**kwargs` passthrough; both modules import via `from scripts.git_utils import git as _git` — `scripts/git_utils.py`, `scripts/self_improve.py`, `scripts/worktree.py` — *Unanimous*

---

### Agenda Item 3: CRLF line endings in `parse_main_worktree`

**Proposals:**
- Dr. Petrov (Systems Architect): On Windows, `git worktree list --porcelain` output may use CRLF (`\r\n`). Splitting on `"\n\n"` works, but `splitlines()` on individual lines still leaves trailing `\r` on the path and branch values, causing subtle path comparison failures.
- Dr. Al-Rashid (Safety): Normalize the full stdout before splitting: `stdout.replace("\r\n", "\n").replace("\r", "\n")`. This is idempotent and prevents any trailing `\r` from propagating into path strings.

**Discussion:** Unanimous. The fix is a one-liner applied before any string operations, and it covers all CRLF variants.

**Decision:** Normalize line endings in `parse_main_worktree` before splitting — `scripts/worktree.py` — *Unanimous*

---

### Agenda Item 4: `run_tests_in_clone` crash on non-UTF-8 output

**Proposals:**
- Dr. Petrov (Systems Architect): Add `errors="replace"` to the `subprocess.run` call in `run_tests_in_clone`. This prevents `UnicodeDecodeError` on Windows when pytest emits bytes outside the UTF-8 range (e.g., Windows-1252 curly quotes from third-party packages).
- Dr. Al-Rashid (Safety): Also guard `result.stdout` for `None`: use `(result.stdout or "")` in the count loop. Although `capture_output=True` with `text=True` should always give a string, defensive coding against `None` prevents `AttributeError` on unexpected edge cases.
- Dr. Osei-Bonsu (Research): The `errors="replace"` approach is correct for this use case — we only need the pass/fail return code and the passed count; corrupted characters in test names or output are irrelevant.

**Discussion:** Unanimous on both changes. `errors="replace"` is the minimal fix; the `None` guard is cheap insurance.

**Decision:** Add `errors="replace"` and guard `result.stdout or ""` in `run_tests_in_clone` — `scripts/self_improve.py` — *Unanimous*

---

## Decisions Log
1. Fix stash detection: `stash_result.stdout.strip().startswith("Saved working directory")` — `scripts/self_improve.py`
2. Extract `_git()` to `scripts/git_utils.py`, both modules import from there — `scripts/git_utils.py`, `scripts/self_improve.py`, `scripts/worktree.py`
3. Normalize CRLF in `parse_main_worktree` before splitting — `scripts/worktree.py`
4. Add `errors="replace"` and `stdout or ""` guard in `run_tests_in_clone` — `scripts/self_improve.py`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Fix stash detection positive match | `scripts/self_improve.py` | Dr. Petrov |
| 2 | Create `scripts/git_utils.py`, update imports | `scripts/git_utils.py`, `scripts/self_improve.py`, `scripts/worktree.py` | Dr. Petrov |
| 3 | CRLF normalization in `parse_main_worktree` | `scripts/worktree.py` | Dr. Al-Rashid |
| 4 | `errors="replace"` + stdout guard | `scripts/self_improve.py` | Dr. Al-Rashid |
