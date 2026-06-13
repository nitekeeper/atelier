"""ResultJournal — deterministic, clock/RNG-free content-addressed result cache.

Standalone, stdlib-only.  Lifted from the M0 PoC (spikes/sdk_host_poc/journal.py)
and adapted for production use.

Key construction
----------------
The journal key is::

    sha256(
        task_id
        ‖ persona
        ‖ phase
        ‖ model
        ‖ canonical(briefing)            # briefing rendered deterministically
        ‖ direct_upstream_digest
    )

where ``‖`` is an injective, length-prefixed concatenation (each field is
serialised as ``len(field_bytes).to_bytes(8, "big") + field_bytes``) so no
choice of field values can collide across field boundaries, and::

    direct_upstream_digest = sha256(
        ‖.join(sorted(direct_reads_from_envelope_hashes))
    )

**Content-chaining, not transitive-closure computation.**  This module does NOT
walk the DAG or compute a transitive closure.  It hashes the *direct* reads-from
upstream envelope hashes that the host driver passes in ``upstream_envelope_hashes``
(sourced from ``dag.validate_dag``'s reads-from relation).  Transitivity is then a
*property* of content-chaining, exactly like a build system: a changed ancestor
invalidates a descendant **only when** it changes an intermediate task's OUTPUT
envelope — which is correct, not a stale-replay bug.  If an intermediate re-emits
byte-identical output after an ancestor change, the descendant's real input is
unchanged and it *correctly* replays from cache.  A task's key therefore always
reflects its actual inputs (its own briefing + its direct upstreams' outputs).

**Clock-free and RNG-free by construction.**  The key may only incorporate
content that is stable across runs given identical inputs.  ``attempt`` is
*not* part of the key (a fresh attempt for the same inputs should replay the
same cached result).

Host-driver contract (for M2/M3)
--------------------------------
The host driver MUST pass each task's **direct** reads-from upstream envelope
hashes — i.e. for every task ``t``, the ``envelope_hash`` of each task whose
output ``t`` reads (the reads-from relation that ``dag.validate_dag`` already
proves), and nothing more.  It must NOT pre-expand a transitive closure: doing
so would over-invalidate (an ancestor change would re-key a descendant even when
the intermediate output was byte-stable).  M1 wires no caller — this is a
contract note so M3 implements the relation correctly.

Persistence
-----------
The journal persists to a JSON file whose path is supplied at construction.
``None`` means in-memory only (useful in tests).  The file is written
atomically (tmp + rename) to survive interrupted runs.

The file format is a JSON object mapping hex-digest key → ``{"envelope": ...,
"usage": ..., "envelope_hash": ...}``.  It is append-on-put (load then merge).

Corrupt or malformed files are silently discarded — the journal starts fresh
rather than crashing the host.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _sha256_hex(*parts: str) -> str:
    """Compute sha256 over injectively-encoded parts; return lowercase hex digest.

    Each part is length-prefixed (``len(bytes).to_bytes(8, "big") + bytes``)
    before hashing.  This makes the concatenation *injective*: there is no pair
    of distinct part-tuples that produce the same byte stream, so embedding the
    delimiter byte (or any byte) inside a field cannot cause a cross-field
    collision (e.g. ``("A\\x00B", "P")`` and ``("A", "B\\x00P")`` hash differently).
    """
    h = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
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

        The key incorporates the task's identity, its model, its rendered
        briefing, and the **direct** reads-from upstream envelope hashes.
        Transitivity is achieved by content-chaining (see the module docstring),
        NOT by computing a transitive closure here — a changed ancestor
        invalidates this task only insofar as it changes a direct upstream's
        output envelope.

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
            Fully-rendered briefing / system prompt.  **Byte-stability is the
            CALLER's obligation:** the key hashes the rendered bytes, so the
            briefing renderer must produce byte-identical output for identical
            inputs (consistent unicode normalization, no timestamps, no RNG,
            stable ordering).  A non-deterministic renderer defeats replay.
        upstream_envelope_hashes:
            The DIRECT reads-from upstream envelope-hash strings — the host
            passes the reads-from set from the validated DAG (NOT a pre-expanded
            transitive closure).  Drives content-chaining: if a direct upstream's
            OUTPUT envelope changes, its hash changes, this task's key changes,
            and the cache misses (a re-dispatch).  Order-independent (sorted
            before hashing).
        """
        task_id = str(task.get("task_id", ""))
        persona = str(task.get("assigned_persona", ""))
        phase = str(task.get("phase", ""))
        upstream_digest = self._upstream_digest(upstream_envelope_hashes or [])
        return _sha256_hex(task_id, persona, phase, model, briefing, upstream_digest)

    def _upstream_digest(self, hashes: list[str]) -> str:
        """sha256 over the sorted direct-upstream envelope hashes.

        Sorting ensures the digest is independent of dispatch order — only
        *which* upstreams ran matters, not when they ran.  Each hash is passed
        as its own length-prefixed part (via :func:`_sha256_hex`) so the digest
        is injective in the multiset of upstream hashes.
        """
        if not hashes:
            return ""
        return _sha256_hex(*sorted(hashes))

    # ── envelope hashing ──────────────────────────────────────────────────

    def envelope_hash(self, envelope: dict[str, Any]) -> str:
        """Stable sha256 of a (serialised) envelope.

        Uses ``json.dumps`` with sorted keys for determinism.  The resulting hex
        digest is what gets stored under ``"envelope_hash"`` and what directly
        downstream tasks include in their ``upstream_envelope_hashes``.

        Note: byte-stability of the digest depends on the envelope being
        serialisable to byte-stable JSON for identical content (sorted keys
        handle key order; the caller is responsible for stable value content,
        e.g. consistent unicode normalization of any string fields).
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
            Validated envelope dict (from the CLI structured_output response).
        usage:
            Usage dict from the CLI result JSON (``input_tokens``, ``output_tokens``, …).
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
