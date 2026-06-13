"""ResultJournal — deterministic, clock/RNG-free content-addressed result cache.

Standalone, stdlib-only.  Lifted verbatim into M1.

Key construction
----------------
The journal key is::

    sha256(
        task.id
        + "\\x00" + persona
        + "\\x00" + phase
        + "\\x00" + model
        + "\\x00" + canonical(briefing)   # briefing rendered deterministically
        + "\\x00" + upstream_results_digest
    )

where::

    upstream_results_digest = sha256(
        "\\x00".join(sorted(envelope_hash(journal[u]) for u in upstream_envelope_hashes))
    )

**Clock-free and RNG-free by construction.**  The key may only incorporate
content that is stable across runs given identical inputs.  ``attempt`` is
*not* part of the key (a fresh attempt for the same inputs should replay the
same cached result).

Persistence
-----------
The journal persists to a JSON file whose path is supplied at construction.
``None`` means in-memory only (useful in tests).  The file is written
atomically (tmp + rename) to survive interrupted runs.

The file format is a JSON object mapping hex-digest key → ``{"envelope": ...,
"usage": ..., "envelope_hash": ...}``.  It is append-on-put (load then merge).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _sha256_hex(*parts: str) -> str:
    """Compute sha256 over NUL-joined parts; return lowercase hex digest."""
    h = hashlib.sha256()
    for i, part in enumerate(parts):
        if i > 0:
            h.update(b"\x00")
        h.update(part.encode("utf-8"))
    return h.hexdigest()


class ResultJournal:
    """In-memory (+ optional JSON file) result journal keyed by deterministic
    content hash.

    Parameters
    ----------
    path:
        Path to the backing JSON file.  ``None`` → in-memory only.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path: Path | None = Path(path) if path is not None else None
        self._store: dict[str, dict[str, Any]] = {}
        if self._path is not None and self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._store = data
            except (json.JSONDecodeError, OSError):
                pass  # corrupt / missing — start fresh

    # ── key construction ───────────────────────────────────────────────────

    def key(
        self,
        task: dict[str, Any],
        attempt: int,
        *,
        model: str,
        briefing: str,
        upstream_envelope_hashes: list[str] | None = None,
    ) -> str:
        """Build the deterministic journal key for one dispatch.

        Parameters
        ----------
        task:
            Task dict with at least ``task_id``, ``assigned_persona``, ``phase``.
        attempt:
            Accepted for call-site symmetry but intentionally NOT part of the
            key: a retry of the same task with the same inputs should replay the
            cached result.
        model:
            Model identifier string (e.g. ``"claude-sonnet-4-5"``).
        briefing:
            Fully-rendered briefing / system prompt (byte-stable across runs for
            identical inputs — callers MUST not include timestamps/RNG).
        upstream_envelope_hashes:
            Sorted envelope-hash strings of transitive upstream dependencies.
            Drives cascade invalidation: if any upstream output changes, the
            downstream key changes and forces a cache miss.
        """
        task_id = str(task.get("task_id", ""))
        persona = str(task.get("assigned_persona", ""))
        phase = str(task.get("phase", ""))
        upstream_digest = self._upstream_digest(upstream_envelope_hashes or [])
        return _sha256_hex(task_id, persona, phase, model, briefing, upstream_digest)

    def _upstream_digest(self, hashes: list[str]) -> str:
        """sha256 over the sorted upstream envelope hashes (NUL-joined).

        Sorting ensures the digest is independent of dispatch order — only
        *which* upstreams ran matters, not when they ran.
        """
        if not hashes:
            return ""
        joined = "\x00".join(sorted(hashes))
        return _sha256_hex(joined)

    # ── envelope hashing ──────────────────────────────────────────────────

    def envelope_hash(self, envelope: dict[str, Any]) -> str:
        """Stable sha256 of a (serialised) envelope.

        Uses ``json.dumps`` with sorted keys for determinism.  The resulting hex
        digest is what gets stored under ``"envelope_hash"`` and what downstream
        tasks include in their ``upstream_envelope_hashes``.
        """
        serialised = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
        return _sha256_hex(serialised)

    # ── lookup + put ──────────────────────────────────────────────────────

    def lookup(self, key: str) -> dict[str, Any] | None:
        """Return the cached envelope for *key*, or ``None`` on a miss."""
        row = self._store.get(key)
        if row is None:
            return None
        return row["envelope"]

    def put(self, key: str, envelope: dict[str, Any], usage: dict[str, Any]) -> None:
        """Store *envelope* under *key* and persist to the backing file.

        Parameters
        ----------
        key:
            Key produced by :meth:`key`.
        envelope:
            Validated envelope dict (from ``validate_envelope``).
        usage:
            ``ResultMessage.usage`` dict (``input_tokens``, ``output_tokens``, …).
        """
        eh = self.envelope_hash(envelope)
        self._store[key] = {
            "envelope": envelope,
            "usage": usage,
            "envelope_hash": eh,
        }
        self._persist()

    def get_envelope_hash(self, key: str) -> str | None:
        """Return the stored envelope_hash for *key*, or ``None`` on a miss."""
        row = self._store.get(key)
        if row is None:
            return None
        return row["envelope_hash"]

    # ── persistence ───────────────────────────────────────────────────────

    def _persist(self) -> None:
        """Atomically write the in-memory store to the backing JSON file."""
        if self._path is None:
            return
        data = json.dumps(self._store, sort_keys=True, indent=2, ensure_ascii=False)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=".journal_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp_path, self._path)
        except Exception:
            import contextlib

            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
