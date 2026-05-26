"""Tests for ``scripts.dispatch.sanitize_bridge_field``.

Contract (mirrored in dispatch.py docstring):
  * Strip all C0 control chars in ``\\x00-\\x08`` + ``\\x0b-\\x1f``.
  * Preserve TAB (``\\x09``), LF (``\\x0a``), CR (``\\x0d``).
  * Reject non-str input with a ``TypeError`` — callers must not
    accidentally pass bytes (UTF-8 vs Latin-1 ambiguity) or ``None``.
"""

from __future__ import annotations

import pytest

from scripts.dispatch import sanitize_bridge_field


def test_sanitize_strips_c0_controls() -> None:
    """C0 chars other than TAB/LF/CR are removed verbatim. ``\\x01``
    (Start-of-Heading) and ``\\x07`` (Bell) are both inside the strip
    range; the surrounding ASCII letters are preserved unchanged."""
    assert sanitize_bridge_field("hello\x01world\x07") == "helloworld"


def test_sanitize_preserves_tab_lf_cr() -> None:
    """TAB / LF / CR are legitimate prompt content (Markdown
    indentation, line breaks) — they MUST survive the sanitiser
    unchanged or the rendered briefing collapses into a single line."""
    src = "a\tb\nc\rd"
    assert sanitize_bridge_field(src) == src


def test_sanitize_rejects_bytes_input() -> None:
    """A ``bytes`` payload must raise ``TypeError`` synchronously.
    Silent acceptance would let UTF-8 vs Latin-1 ambiguity propagate
    into the template render where the diagnostic surface is far
    worse (a Jinja2 traceback inside a worker spawn prompt)."""
    with pytest.raises(TypeError):
        sanitize_bridge_field(b"hello")


def test_sanitize_rejects_none_input() -> None:
    """``None`` is also rejected — same reasoning as bytes. The docstring
    explicitly calls out ``None`` as a forbidden input alongside bytes."""
    with pytest.raises(TypeError):
        sanitize_bridge_field(None)  # type: ignore[arg-type]
