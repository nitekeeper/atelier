#!/usr/bin/env python3
"""Atelier token-lever benchmark — ponytail's agentic methodology applied to
atelier's REAL rule constants.

Each cell = one headless `claude -p` editing an isolated copy of a fixture repo,
scored on the git-diff it leaves behind. Arms inject atelier's actual briefing
rules via --append-system-prompt:

  bare          baseline — task only (the #1 comparison)
  terse         + the terse/"caveman" output rule (ponytail's input-side lever)
  minimal_diff  + atelier ``_MINIMAL_DIFF_RULE`` (the output-side lever, live)
  both          + both rules

NOTE ON THE ``terse`` ARM: atelier's terse rule (B1) was measured a net loss at
every tier and has been **deleted from production** (see
``results/2026-06-20-*.md``). It survives here ONLY as ``_FROZEN_TERSE_RULE`` — a
frozen copy of the exact deleted text — so this benchmark stays self-contained and
the keep/kill A/B remains reproducible. The benchmark imports nothing terse from
atelier; ``_MINIMAL_DIFF_RULE`` is still imported live from ``scripts.dispatch``.

Metrics (per cell): git-diff added LOC (the +N a PR shows), four-channel tokens
(output+input+cache_creation+cache_read), cost_usd, wall-ms, and an LLM
over-engineering/correctness judge; plus a deterministic adversarial safety check
on the safe-join task.

Usage:
  python3 run.py --selftest-offline   # deterministic instruments only; NO API (CI)
  python3 run.py --selftest           # full instrument proof incl. judge (small $)
  python3 run.py --reps 2             # run the live matrix
  python3 run.py --arms bare,minimal_diff --tasks safe-join
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median

BENCH = Path(__file__).resolve().parent
REPO_ROOT = BENCH.parent  # atelier repo root (benchmarks/ lives at the top level)
FIXTURE = BENCH / "fixtures" / "widget-app"
RUNS = BENCH / "runs"
CELL_TIMEOUT_S = 600
MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-6"}
JUDGE_MODEL = "claude-sonnet-4-6"
MAX_WORKERS = 6

sys.path.insert(0, str(REPO_ROOT))
from scripts.dispatch import _MINIMAL_DIFF_RULE  # noqa: E402

# ── frozen terse rule ─────────────────────────────────────────────────────────
# Atelier's terse/"caveman" output rule (B1), DELETED from production as a measured
# net loss (results/2026-06-20-*.md). Frozen here verbatim so the A/B arm still
# reproduces the finding without a live import of a now-removed constant. Do NOT
# "improve" this text — its value is being the exact thing that was measured.
_FROZEN_TERSE_RULE = (
    "\n\n# OUTPUT SHAPE (terse — read last)\n\n"
    "Keep your free-text `notes_md` prose COMPACT to save the orchestrator's "
    "context. Talk like a smart caveman — brain stays big, only fluff dies. Use "
    "fragments, one line per finding/decision, no pleasantries, no hedging, no "
    "restating this briefing. Drop articles and filler where meaning survives. "
    "This applies ONLY to your free-text prose. Do NOT compress or alter, and "
    "reproduce VERBATIM: the TM-006 reply envelope (the `task_result` / "
    "`shutdown_response` JSON and ALL its keys/values), code (fenced or inline), "
    "file paths, identifiers (CONST_CASE, dotted.names, fn() calls), version "
    "numbers, quoted error strings, and the `ABANDON: <category>:<reason>` "
    "grammar line. Those stay byte-exact. If terseness would create technical "
    "ambiguity (security, destructive/irreversible actions, ordered multi-step "
    "sequences), write that part in full."
)

# ── arms ────────────────────────────────────────────────────────────────────
ARMS: dict[str, str | None] = {
    "bare": None,
    "terse": _FROZEN_TERSE_RULE.strip(),
    "minimal_diff": _MINIMAL_DIFF_RULE.strip(),
    "both": (_FROZEN_TERSE_RULE + _MINIMAL_DIFF_RULE).strip(),
}

# ── tasks (ponytail two-tier: over-build-prone, irreducible, safety) ──────────
TASKS: list[dict] = [
    {
        "id": "date-field",
        "file": "src/signup.html",
        "overbuild_prone": True,
        "prompt": (
            "Add a date-of-birth field to the signup form in src/signup.html, "
            "where the TODO marker is. The user must be able to pick their date of birth."
        ),
    },
    {
        "id": "slugify",
        "file": "pyutil/text.py",
        "overbuild_prone": False,
        "prompt": (
            "In pyutil/text.py, implement slugify(title: str) -> str that converts a "
            "title into a lowercase URL slug: keep alphanumerics, collapse runs of "
            "non-alphanumerics into single hyphens, strip leading/trailing hyphens."
        ),
    },
    {
        "id": "safe-join",
        "file": "pyutil/files.py",
        "overbuild_prone": False,
        "safety": True,
        "prompt": (
            "In pyutil/files.py, implement safe_join(base_dir: str, user_filename: str) "
            "-> str returning the absolute path of user_filename inside base_dir. "
            "user_filename comes from untrusted client input."
        ),
    },
    {
        "id": "file-dropzone",
        "file": "src/signup.html",
        "overbuild_prone": True,
        "prompt": (
            "Add a profile-photo upload field to the signup form in src/signup.html. "
            "Users need to attach an image file when signing up."
        ),
    },
    {
        "id": "form-validation",
        "file": "src/signup.html",
        "overbuild_prone": True,
        "prompt": (
            "Add client-side validation to the signup form in src/signup.html: the email "
            "must be a valid email address and the password at least 8 characters, with the "
            "user prevented from submitting an invalid form."
        ),
    },
]


# ── cell execution ───────────────────────────────────────────────────────────
def _git(workdir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=b@b", "-c", "user.name=b", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def added_loc(workdir: Path) -> int:
    """Sum of git-diff ADDED lines across source files (the +N a PR shows)."""
    _git(workdir, "add", "-A")
    numstat = _git(workdir, "diff", "--cached", "--numstat", "HEAD")
    total = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            f = parts[2]
            if f.endswith((".lock", ".min.js")) or "package-lock" in f:
                continue
            total += int(parts[0])
    return total


def run_cell(task: dict, arm: str, rep: int, model: str) -> dict:
    """One isolated `claude -p` cell. Returns the scored result dict."""
    workdir = Path(tempfile.mkdtemp(prefix=f"cell-{task['id']}-{arm}-{model}-{rep}-"))
    cell = {"task": task["id"], "arm": arm, "rep": rep, "model": model}
    try:
        # fresh fixture copy + git baseline (so the diff is exactly this cell's work)
        shutil.copytree(FIXTURE, workdir / "repo", dirs_exist_ok=True)
        repo = workdir / "repo"
        shutil.rmtree(repo / ".git", ignore_errors=True)
        _git(repo, "init")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "baseline")

        argv = [
            "claude",
            "-p",
            task["prompt"],
            "--model",
            MODELS[model],
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--disallowedTools",
            "Bash",
            "WebFetch",
            "WebSearch",
        ]
        rule = ARMS[arm]
        if rule:
            argv += ["--append-system-prompt", rule]

        out_path = workdir / "_claude.json"  # file, not PIPE (hung-child safety)
        t0 = time.time()
        with open(out_path, "w") as fh:
            proc = subprocess.run(
                argv,
                cwd=repo,
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
                timeout=CELL_TIMEOUT_S,
                check=False,
            )
        cell["wall_ms"] = int((time.time() - t0) * 1000)
        if proc.returncode != 0:
            cell["error"] = f"claude rc={proc.returncode}: {proc.stderr[:200]}"
            return cell

        res = json.loads(out_path.read_text())
        u = res.get("usage", {}) or {}
        cell["tokens"] = (
            int(u.get("output_tokens", 0))
            + int(u.get("input_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0))
            + int(u.get("cache_read_input_tokens", 0))
        )
        cell["output_tokens"] = int(u.get("output_tokens", 0))
        cell["cost_usd"] = res.get("total_cost_usd")
        cell["loc"] = added_loc(repo)
        cell["wrote_file"] = cell["loc"] > 0

        diff = _git(repo, "diff", "--cached", "HEAD")
        cell["diff_chars"] = len(diff)
        if task.get("safety"):
            cell["safe"] = _safety_check(repo)
        cell["judge"] = _judge(task, diff)
        return cell
    except subprocess.TimeoutExpired:
        cell["error"] = "cell timeout"
        return cell
    except Exception as e:
        cell["error"] = f"{type(e).__name__}: {e}"
        return cell
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _safety_check(repo: Path) -> bool | None:
    """Execute the produced safe_join against an adversarial traversal input.
    True = contained (safe); False = escaped (unsafe); None = unimportable."""
    import importlib.util
    import os

    target = repo / "pyutil" / "files.py"
    try:
        spec = importlib.util.spec_from_file_location("_bench_files", target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "safe_join", None)
        if fn is None:
            return None
        base = tempfile.mkdtemp()
        try:
            got = fn(base, "../../../../etc/passwd")
        except Exception:
            return True  # rejecting the traversal is safe
        real = os.path.realpath(str(got))
        return os.path.commonpath([real, os.path.realpath(base)]) == os.path.realpath(base)
    except Exception:
        return None


_JUDGE_RUBRIC = (
    "You are scoring a single git diff that an AI agent produced for a feature ticket. "
    'Return ONLY a JSON object: {"over_engineering": 0..3, "correctness": 0..3, "offending": "..."}. '
    "over_engineering: 0=minimal/idiomatic, 1=slightly more than needed, 2=notable extra "
    "abstraction/deps/config, 3=framework-for-a-one-off. correctness: 0=does not satisfy the "
    "ticket, 1=partial, 2=works on the happy path, 3=correct incl. edge cases. "
    "'offending' names the single most over-built construct, or 'none'."
)


def _judge(task: dict, diff: str) -> dict:
    prompt = (
        f"{_JUDGE_RUBRIC}\n\nTICKET: {task['prompt']}\n\nDIFF:\n{diff[:6000]}\n\n"
        "Return only the JSON object."
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", JUDGE_MODEL, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        res = json.loads(proc.stdout)
        text = res.get("result", "")
        s = text[text.find("{") : text.rfind("}") + 1]
        return json.loads(s)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── selftest (prove the instrument before spending) ──────────────────────────
GOOD_SAFE = (
    "import os\n\n"
    "def safe_join(base_dir, user_filename):\n"
    "    base = os.path.realpath(base_dir)\n"
    "    full = os.path.realpath(os.path.join(base, user_filename))\n"
    "    if os.path.commonpath([full, base]) != base:\n"
    "        raise ValueError('path traversal')\n"
    "    return full\n"
)
BAD_SAFE = (
    "import os\n\n"
    "def safe_join(base_dir, user_filename):\n"
    "    return os.path.join(base_dir, user_filename)\n"
)


def selftest_offline() -> bool:
    """DETERMINISTIC instrument proof — NO API spend (the CI gate). Validates the
    adversarial safety scorer discriminates a safe impl from an unsafe one, and
    that the four arms wire the right rule text. Returns True on FAILURE (mirrors
    ponytail's ``if selftest(): sys.exit``)."""
    ok = True
    # 1. safety scorer: good contains, bad escapes
    for label, code, want in (("good", GOOD_SAFE, True), ("bad", BAD_SAFE, False)):
        d = Path(tempfile.mkdtemp())
        (d / "pyutil").mkdir()
        (d / "pyutil" / "files.py").write_text(code)
        got = _safety_check(d)
        shutil.rmtree(d, ignore_errors=True)
        print(f"  selftest safety[{label}]: got={got} want={want}", "OK" if got == want else "FAIL")
        ok = ok and (got == want)
    # 2. arm wiring: bare is None; terse/minimal_diff/both carry their rule text
    wiring = (
        ARMS["bare"] is None
        and ARMS["terse"]
        and "OUTPUT SHAPE (terse" in ARMS["terse"]
        and ARMS["minimal_diff"]
        and "minimal-diff" in ARMS["minimal_diff"]
        and ARMS["both"]
        and "OUTPUT SHAPE (terse" in ARMS["both"]
        and "minimal-diff" in ARMS["both"]
    )
    print("  selftest arm-wiring:", "OK" if wiring else "FAIL")
    ok = ok and bool(wiring)
    return not ok  # True == broken


def selftest() -> bool:
    """Full instrument proof: the deterministic offline checks PLUS the LLM judge
    discrimination (a minimal diff must score lower over-engineering than a bloated
    one). Spends a little on the judge. Returns True on FAILURE."""
    ok = not selftest_offline()
    task = TASKS[0]
    minimal = '+<input name="dob" type="date">\n'
    bloated = "+" + "\n+".join(f"// custom date picker line {i}" for i in range(60))
    jm = _judge(task, minimal)
    jb = _judge(task, bloated)
    print(
        f"  selftest judge: minimal.over_eng={jm.get('over_engineering')} bloated.over_eng={jb.get('over_engineering')}"
    )
    discriminates = (
        isinstance(jm.get("over_engineering"), int)
        and isinstance(jb.get("over_engineering"), int)
        and jm["over_engineering"] < jb["over_engineering"]
    )
    print("  selftest judge discriminates:", "OK" if discriminates else "FAIL")
    ok = ok and discriminates
    return not ok  # True == broken


# ── matrix + aggregate ───────────────────────────────────────────────────────
def aggregate(cells: list[dict]) -> list[dict]:
    rows = []
    keys = {(c["task"], c["arm"], c.get("model", "?")) for c in cells if "error" not in c}
    for tid, arm, model in sorted(keys):
        cs = [
            c
            for c in cells
            if c["task"] == tid
            and c["arm"] == arm
            and c.get("model", "?") == model
            and "error" not in c
        ]
        if not cs:
            continue
        wrote = [c for c in cs if c.get("wrote_file")]
        loc = [c["loc"] for c in wrote] or [0]
        row = {
            "task": tid,
            "arm": arm,
            "model": model,
            "n": len(cs),
            "loc_median": median(loc),
            "tokens_mean": round(sum(c["tokens"] for c in cs) / len(cs)),
            "cost_mean": round(sum(c.get("cost_usd") or 0 for c in cs) / len(cs), 4),
            "wall_ms_mean": round(sum(c["wall_ms"] for c in cs) / len(cs)),
            "over_eng_mean": round(
                sum(
                    c["judge"].get("over_engineering", 0)
                    for c in cs
                    if isinstance(c.get("judge"), dict)
                )
                / len(cs),
                2,
            ),
            "correct_mean": round(
                sum(
                    c["judge"].get("correctness", 0) for c in cs if isinstance(c.get("judge"), dict)
                )
                / len(cs),
                2,
            ),
            "wrote_rate": round(len(wrote) / len(cs), 2),
        }
        safes = [c["safe"] for c in cs if "safe" in c and c["safe"] is not None]
        if safes:
            row["safe_rate"] = round(sum(1 for s in safes if s) / len(safes), 2)
        rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--selftest-offline",
        action="store_true",
        help="deterministic instrument proof only; NO API spend (the CI gate)",
    )
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="full instrument proof incl. the LLM judge (small spend)",
    )
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--tasks", default=",".join(t["id"] for t in TASKS))
    ap.add_argument("--models", default="haiku,sonnet")
    ap.add_argument("--stamp", default="run")
    a = ap.parse_args(argv)

    if a.selftest_offline:
        if selftest_offline():
            print("INSTRUMENTS BROKEN (offline).")
            return 1
        print("selftest-offline OK — deterministic instruments validated (no API spend).")
        return 0

    if selftest():
        print("INSTRUMENTS BROKEN — refusing to spend on the live matrix.")
        return 1
    print("selftest OK — instruments validated.")
    if a.selftest:
        return 0

    arms = [x for x in a.arms.split(",") if x in ARMS]
    tasks = [t for t in TASKS if t["id"] in a.tasks.split(",")]
    models = [m for m in a.models.split(",") if m in MODELS]
    matrix = [(t, arm, r, m) for t in tasks for arm in arms for m in models for r in range(a.reps)]
    print(
        f"matrix: {len(tasks)} tasks x {len(arms)} arms x {len(models)} models x {a.reps} reps = {len(matrix)} cells"
    )

    cells: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(run_cell, t, arm, r, m): (t["id"], arm, r, m) for t, arm, r, m in matrix}
        for fut in as_completed(futs):
            c = fut.result()
            cells.append(c)
            tag = (
                "ERR " + c.get("error", "")[:40]
                if "error" in c
                else f"loc={c.get('loc')} tok={c.get('tokens')}"
            )
            print(
                f"  [{len(cells)}/{len(matrix)}] {c['task']}/{c['arm']}/{c.get('model')}#{c['rep']}: {tag}"
            )

    rows = aggregate(cells)
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{a.stamp}.json"
    out.write_text(json.dumps({"cells": cells, "aggregate": rows}, indent=2))
    print(f"\nwrote {out}\n")
    _print_table(rows)
    return 0


def _print_table(rows: list[dict]) -> None:
    cols = [
        "task",
        "arm",
        "model",
        "n",
        "loc_median",
        "tokens_mean",
        "cost_mean",
        "wall_ms_mean",
        "over_eng_mean",
        "correct_mean",
        "safe_rate",
    ]
    print(" | ".join(c.ljust(12) for c in cols))
    for r in rows:
        print(" | ".join(str(r.get(c, "-")).ljust(12) for c in cols))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
