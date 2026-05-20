"""Lint test: forbid bare ``python`` invocations in documented commands.

On machines that ship only ``python3`` (no ``python`` symlink), every
documented command of the form ``python scripts/foo.py`` fails with
``command not found``. This lint walks the policed directories and asserts
no such invocations remain.

What this catches:
    - ``python atelier/scripts/workflow.py ...``
    - ``PYTHONPATH=. python -m scripts.foo``
    - ``python /path/to/hook.py`` in README examples
    - Bare ``python`` in docstring usage examples inside ``scripts/*.py``

What this deliberately ignores:
    - ``python3`` (already portable)
    - ``PYTHONPATH``, ``pythonic``, ``python_path`` (substring false positives)
    - Prose references like ``Python 3.11+``, ``Python is``, ``Python.``
    - Shebangs ``#!/usr/bin/env python3`` / ``#!/usr/bin/python3``
    - CI workflow files (GitHub runners resolve ``python`` reliably)
    - ``pyproject.toml`` version declarations (e.g. ``py310``)
    - This test file itself (its assertion strings must mention ``python``)
    - Markdown fenced code block language tags (e.g. ``` ```python ``` ``) —
      these are syntax-highlighting hints, not shell invocations
"""

from __future__ import annotations

import re
from pathlib import Path

# Repository root (parent of tests/).
REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard-coded, deterministic include list. New top-level dirs do not silently
# join the lint scope; that is intentional.
POLICED_DIRS = [
    REPO_ROOT / "skills",
    REPO_ROOT / "internal",
    REPO_ROOT / "scripts",
    REPO_ROOT / "hooks",
]

# Specific files outside the policed dirs that are still in scope.
POLICED_FILES = [
    REPO_ROOT / "README.md",
]

# Only inspect text formats where documented commands typically live.
POLICED_SUFFIXES = {".md", ".py"}

# Files that legitimately reference the bug or assertion strings.
EXEMPT_PATHS = {
    Path(__file__).resolve(),
}

# Substrings whose presence on a line indicates the regex hit is a
# false positive (substring matches, prose, shebangs, etc.).
EXCLUSION_SUBSTRINGS = (
    "PYTHONPATH",
    "pythonic",
    "python_path",
    "Python ",      # prose: "Python 3.11+", "Python is"
    "Python.",      # prose: "Python."
    "Python,",      # prose: "Python, ..."
    "python3",      # already correct
    "cpython",
    "micropython",
    "#!/usr/bin/env python3",
    "#!/usr/bin/python3",
)

# Match the literal word ``python`` not followed by ``3`` (and not part of
# a longer identifier like ``pythonic``). The trailing negative lookahead
# rejects ``python3`` and the leading word boundary rejects ``cpython``.
BARE_PYTHON_RE = re.compile(r"\bpython\b(?!3)")

# Markdown fenced code block opener with ``python`` as the syntax-highlight
# language tag (e.g. ``` ```python ``` ``). Whitespace-only prefix allowed
# for indented fenced blocks inside list items.
FENCED_CODE_LANG_RE = re.compile(r"^\s*```python\s*$")


def _iter_policed_files():
    seen = set()
    for d in POLICED_DIRS:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in POLICED_SUFFIXES:
                rp = p.resolve()
                if rp in EXEMPT_PATHS or rp in seen:
                    continue
                seen.add(rp)
                yield rp
    for f in POLICED_FILES:
        if f.is_file():
            rp = f.resolve()
            if rp in EXEMPT_PATHS or rp in seen:
                continue
            seen.add(rp)
            yield rp


def _line_is_excluded(line: str) -> bool:
    if FENCED_CODE_LANG_RE.match(line):
        return True
    return any(sub in line for sub in EXCLUSION_SUBSTRINGS)


def _find_bare_python_offenders():
    offenders = []
    for path in _iter_policed_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if not BARE_PYTHON_RE.search(line):
                continue
            if _line_is_excluded(line):
                continue
            offenders.append((str(path.relative_to(REPO_ROOT)), i, line.rstrip()))
    return offenders


def test_no_bare_python_invocations_in_docs():
    offenders = _find_bare_python_offenders()
    if offenders:
        formatted = "\n".join(
            f"  {path}:{lineno} - {content}" for path, lineno, content in offenders
        )
        raise AssertionError(
            "Found bare 'python' invocations in policed files. "
            "Use 'python3' instead so documented commands work on systems "
            "without a 'python' symlink.\n\n"
            f"Offending lines ({len(offenders)} total):\n{formatted}"
        )
