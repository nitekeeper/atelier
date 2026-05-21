"""One-command version bump.

Atelier version is held in THREE places that must stay in lockstep:

  1. `.claude-plugin/plugin.json` — canonical manifest read by Claude Code.
  2. `pyproject.toml` — top-level `version` field (Ruff/tooling read here).
  3. `scripts/bootstrap.py` — `_atelier_version()` fallback sentinel used
     when atelier is not pip-installed (the common case in worktrees).
     If this drifts, the bootstrap marker disagrees with the actual ship
     version and gets re-run unnecessarily — see CHANGELOG v1.2.0 which
     caught up a missed v1.1.1 bump.

Usage:
    python3 -m scripts.bump 1.3.0

Steps:
  1. Validate the version string (X.Y.Z, no leading 'v').
  2. Read the current version from plugin.json — refuse to downgrade or
     bump to the same value.
  3. Update plugin.json: `version` field.
  4. Update pyproject.toml: top-level `version` field.
  5. Update scripts/bootstrap.py: `_atelier_version()` fallback literal.
  6. Remove the previous `dist/v<old>/manifest.json` (gitignored body, only
     manifest is tracked — see .gitignore).
  7. Call `scripts.release.build(new)` to produce `dist/v<new>/manifest.json`.

What this does NOT do (deliberate):
  - Write a CHANGELOG entry — that's editorial work, done by hand.
  - Commit, tag, or push — the release workflow triggers on tag push, but
    the tag itself is a human decision (see .github/workflows/release.yml).
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

from scripts import release

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _parse_version(s: str) -> tuple[int, int, int]:
    if not _VERSION_RE.match(s):
        raise ValueError(f"version must look like X.Y.Z (got {s!r}). Do not include a leading 'v'.")
    return tuple(int(p) for p in s.split("."))  # type: ignore[return-value]


def _read_plugin_json() -> dict:
    return json.loads(Path(".claude-plugin/plugin.json").read_text(encoding="utf-8"))


def _write_plugin_json(data: dict) -> None:
    # Preserve trailing newline + 2-space indent (matches existing file)
    Path(".claude-plugin/plugin.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _update_plugin_json(new: str) -> str:
    data = _read_plugin_json()
    old = data["version"]
    data["version"] = new
    _write_plugin_json(data)
    return old


def _update_pyproject(new: str) -> str:
    path = Path("pyproject.toml")
    content = path.read_text(encoding="utf-8")
    match = re.search(r'^(version\s*=\s*")([^"]+)(")', content, re.MULTILINE)
    if not match:
        # pyproject.toml may legitimately omit a version field (atelier's
        # original pyproject.toml is config-only). Treat missing as a no-op
        # rather than a hard error so atelier doesn't have to grow a
        # pyproject version unless it wants one. Log to stderr so a future
        # mid-cycle addition of a version field doesn't drift silently.
        print(
            "info: pyproject.toml has no [project] version field — skipped",
            file=sys.stderr,
        )
        return ""
    old = match.group(2)
    new_content = content[: match.start(2)] + new + content[match.end(2) :]
    path.write_text(new_content, encoding="utf-8")
    return old


def _update_bootstrap_sentinel(new: str) -> str:
    """Bump the `_atelier_version()` fallback literal in scripts/bootstrap.py.

    The fallback returns a hard-coded string when `importlib.metadata.version`
    fails (atelier is rarely pip-installed). It must track the canonical
    plugin.json version or `bootstrap.is_bootstrapped` returns false every
    invocation and re-bootstraps unnecessarily.
    """
    path = Path("scripts/bootstrap.py")
    content = path.read_text(encoding="utf-8")
    # Look for `return "<X.Y.Z>"` immediately inside _atelier_version's except
    # branch. We anchor on the function name + except + return literal to
    # avoid clobbering unrelated string literals elsewhere in the file.
    pattern = re.compile(
        r"(def _atelier_version\(\) -> str:.*?except Exception:\s*\n\s*return\s*\")([^\"]+)(\")",
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        raise RuntimeError(
            "scripts/bootstrap.py has no _atelier_version() fallback sentinel matching the "
            'expected `return "X.Y.Z"` shape — refusing to bump (would silently drift).'
        )
    old = match.group(2)
    new_content = content[: match.start(2)] + new + content[match.end(2) :]
    path.write_text(new_content, encoding="utf-8")
    return old


def _remove_old_manifest(old: str) -> Path | None:
    p = Path("dist") / f"v{old}" / "manifest.json"
    if p.exists():
        p.unlink()
        # Also remove the empty version dir if it's now empty
        with contextlib.suppress(OSError):
            p.parent.rmdir()
        return p
    return None


def bump(new: str) -> dict:
    """Bump the project to a new version. Returns a summary dict."""
    _parse_version(new)

    current = _read_plugin_json()["version"]
    if _parse_version(new) <= _parse_version(current):
        raise ValueError(
            f"refusing to bump to {new}: not greater than current {current}. "
            "scripts.bump only goes forward."
        )

    old_pj = _update_plugin_json(new)
    old_py = _update_pyproject(new)
    old_boot = _update_bootstrap_sentinel(new)

    drifts = []
    if old_py and old_py != old_pj:
        drifts.append(f"pyproject.toml was {old_py}")
    if old_boot != old_pj:
        drifts.append(f"bootstrap._atelier_version() was {old_boot}")
    if drifts:
        print(
            f"warn: pre-bump drift detected (plugin.json was {old_pj}; "
            f"{'; '.join(drifts)}); all now {new}"
        )

    removed = _remove_old_manifest(old_pj)
    built = release.build(new)

    return {
        "old": old_pj,
        "new": new,
        "removed_manifest": str(removed) if removed else None,
        "built_dist": str(built),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: python3 -m scripts.bump X.Y.Z (got {argv[1:]})", file=sys.stderr)
        return 2
    if argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    try:
        result = bump(argv[1])
    except (ValueError, RuntimeError) as e:
        print(f"bump failed: {e}", file=sys.stderr)
        return 1
    print(f"bumped {result['old']} -> {result['new']}")
    if result["removed_manifest"]:
        print(f"  removed: {result['removed_manifest']}")
    print(f"  built:   {result['built_dist']}")
    print()
    print("Next steps:")
    print(f"  1. Add a CHANGELOG.md entry for v{result['new']} (editorial — done by hand).")
    print("  2. Commit the bump (plugin.json, pyproject.toml, scripts/bootstrap.py, dist/).")
    print(f"  3. After merge: git tag v{result['new']} && git push --tags")
    print("     (the release workflow builds the GitHub Release from there).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
