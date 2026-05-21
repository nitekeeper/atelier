"""Build a dist/v<version>/ bundle for Claude Code plugin distribution.

The bundle includes: .claude-plugin/ (the canonical manifest), scripts/,
skills/, internal/, hooks/, templates/, migrations/, a manifest.json with
file inventory, and INSTALL.md instructions.

dist/ body is gitignored; only manifest tracking is committed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

# Claude Code reads .claude-plugin/plugin.json (the canonical manifest); the
# dist bundle MUST include that directory for `claude --plugin-dir` to work.
#
# Atelier's directory shape (vs memex):
#   - has: hooks/, templates/, migrations/
#   - lacks: db/, prompts/
_INCLUDE_DIRS = [
    ".claude-plugin",
    "scripts",
    "skills",
    "internal",
    "hooks",
    "templates",
    "migrations",
]
_INCLUDE_FILES = ["pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE", "requirements.txt"]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(version: str, target_root: Path | str = "dist") -> Path:
    """Build a dist bundle. Returns the path to the version directory."""
    target_root = Path(target_root)
    version_dir = target_root / f"v{version}"
    if version_dir.exists():
        shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True)

    repo_root = Path.cwd()
    files_manifest: list[dict] = []

    # Copy directories
    for dirname in _INCLUDE_DIRS:
        src = repo_root / dirname
        if not src.exists():
            continue
        dst = version_dir / dirname
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in dst.rglob("*"):
            if f.is_file():
                files_manifest.append(
                    {
                        "path": str(f.relative_to(version_dir)),
                        "sha256": _hash_file(f),
                        "bytes": f.stat().st_size,
                    }
                )

    # Copy individual files
    for fname in _INCLUDE_FILES:
        src = repo_root / fname
        if not src.exists():
            continue
        dst = version_dir / fname
        shutil.copy2(src, dst)
        files_manifest.append(
            {
                "path": fname,
                "sha256": _hash_file(dst),
                "bytes": dst.stat().st_size,
            }
        )

    # INSTALL.md (generated, not copied).
    install_md = f"""# Atelier v{version} Install Instructions

## Fresh install

1. Install via your Claude Code marketplace (agora). The bundle lands under
   `~/.claude/plugins/cache/<marketplace>/atelier/<version>/` (Claude Code
   manages this path — you do not place files manually).
2. Restart Claude Code or invoke `/plugin reload atelier`.
3. Invoke `atelier:run` to start a session. On first invocation, the
   bootstrap detects Atelier's mode (Memex vs Local) and seeds the role +
   agent catalog accordingly.

## Modes

Atelier runs in one of two modes, auto-detected by `scripts.mode_detector`:

- **Memex mode** — Memex v2.6.0+ is installed. Atelier seeds its 61-role
  catalog into `~/.memex/agents.db` and calls
  `memex.ensure_internal_agents()` to restore Memex's internal-agent
  invariant after the touch.
- **Local mode** — No Memex installed. Atelier creates
  `<workspace>/.ai/atelier.db` and seeds roles + agents locally. No
  Memex contact.

Bootstrap writes a marker (`atelier.bootstrap.json`) pinning the
(atelier, memex) version pair. Subsequent invocations skip bootstrap in
O(1) when the marker matches.

## Verifying

After first `atelier:run`:

- Memex mode: query `~/.memex/agents.db` for `roles` row count;
  should return at least 61.
- Local mode: query `<workspace>/.ai/atelier.db` for `roles` row count;
  should return at least 61.

## Skills shipped

Atelier registers a small set of top-level skills (`atelier:run`,
`atelier:save`, `atelier:load`, `atelier:ingest`, `atelier:migrate`).
The bulk of its 19+ internal procedures live at
`internal/<name>/SKILL.md` and are reached on demand from `atelier:run`.

## Manual install (advanced)

If installing from a GitHub Release zip (`gh release download v{version}`)
instead of agora:

1. Unzip into `~/.claude/plugins/cache/local/atelier/{version}/`.
2. Restart Claude Code, or invoke `/plugin reload atelier`.

This bypasses agora's marketplace metadata; updates are manual.
"""
    (version_dir / "INSTALL.md").write_text(install_md, encoding="utf-8")
    files_manifest.append(
        {
            "path": "INSTALL.md",
            "sha256": _hash_file(version_dir / "INSTALL.md"),
            "bytes": (version_dir / "INSTALL.md").stat().st_size,
        }
    )

    # Manifest
    manifest = {
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files_manifest),
        "files": sorted(files_manifest, key=lambda f: f["path"]),
    }
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return version_dir


if __name__ == "__main__":
    import sys

    version = sys.argv[1] if len(sys.argv) > 1 else "1.2.0"
    out = build(version)
    print(f"Built: {out}")
