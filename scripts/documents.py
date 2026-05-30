# scripts/documents.py
"""Project documents — wrapper around backend.write_document and
operational CRUD against the documents-pointer table.

Public surface unchanged from pre-retrofit (`create_document`,
`get_document`, `update_document`, `delete_document`, `list_documents`,
`search_documents`). Internals now call the mode-dispatched backend
(Memex or Local) via `scripts.backend`. The `db_path` first positional
remains for backwards compatibility with existing test fixtures and CLI
callers; the backend determines storage location via mode detection
(spec §4.3).

v1.0.13 `type` column is gone in v1.1.0 (replaced by `domain` +
`subdomain` per spec §6.4). For backwards compatibility:

- `create_document` still takes `type`. We translate via
  `domain_vocabulary.TYPE_TO_DOMAIN`; unknown types fall back to
  ``("project_doc", <type>)`` per spec §11.4. The returned dict carries
  the original `type` key so legacy callers and tests still see it.
- `list_documents(type=...)` filters by the translated `domain` so
  ``type="design"`` still returns design docs.
- `update_document(**kwargs)` accepts the legacy ``type`` kwarg and
  re-maps it through TYPE_TO_DOMAIN so updates land on `domain` /
  `subdomain`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts import backend
from scripts.domain_vocabulary import TYPE_TO_DOMAIN

# The §6.4 storage coordinates of a 9-section PM spec doc: domain=project,
# subdomain=spec (design spec 2026-05-25-atelier-team-mode-design.md §6.4).
# write_spec_amendment uses this pair as the "clean way to tell" a spec doc
# apart from an arbitrary project document.
_SPEC_DOMAIN = "project"
_SPEC_SUBDOMAIN = "spec"

_BARE_WORD = re.compile(r"^[A-Za-z0-9_]+$")


def _maybe_prefix(q: str) -> str:
    """Rewrite a bare-word query to an FTS5 prefix query (``OAuth -> OAuth*``).

    Anything containing whitespace, quotes, FTS5 operators, or boolean
    keywords is passed through unchanged so callers can supply explicit
    FTS5 expressions when literal token matching is desired.
    """
    return f"{q}*" if _BARE_WORD.match(q) else q


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_root() -> Path:
    """Resolve the workspace root.

    Imported lazily to avoid the libtmux preflight that `scripts.workspace`
    runs at import time — Atelier's test environment doesn't always have
    tmux available, and `documents` is reachable from CLI contexts where
    we don't need workspace tmux features.
    """
    from scripts.workspace import workspace_root

    return workspace_root()


def _translate_type(type_str: str) -> tuple[str, str | None]:
    """Translate a v1 free-form `type` to (domain, subdomain).

    Spec §11.4 line 1027: unknown v1 types fall back to
    ``("project_doc", <type>)`` so legacy callers passing arbitrary
    strings still produce a valid Tier 2 write.
    """
    return TYPE_TO_DOMAIN.get(type_str, ("project_doc", type_str))


def create_document(
    db_path: str,
    project_id: int,
    type: str,
    title: str,
    filename: str,
    created_by: str,
    workspace_id: int | None = None,
) -> dict:
    """Register a project document.

    Per spec §6.8, the indexed body MUST be the actual file content —
    placeholder bodies are explicitly forbidden because they make the
    doc undiscoverable, which is worse than a hard error at registration
    time. The caller-supplied `filename` is interpreted relative to
    `workspace_root()`; if it doesn't exist on disk we raise eagerly.

    `db_path` is retained for backwards compatibility with existing test
    fixtures; the backend determines storage location via mode detection.

    Returns a dict shaped like the v1.0.13 record (id, project_id, type,
    title, filename, created_by, created_at, updated_at) plus `index_id`
    (the Memex Index id, or None in Local mode).
    """
    file_path = _workspace_root() / filename
    if not file_path.exists():
        raise FileNotFoundError(
            f"Document file does not exist: {file_path}. "
            f"Create the markdown file first, then register with atelier."
        )
    body = file_path.read_text(encoding="utf-8")
    domain, subdomain = _translate_type(type)
    result = backend.write_document(
        workspace_id=workspace_id,
        project_id=project_id,
        domain=domain,
        subdomain=subdomain,
        title=title,
        body=body,
        metadata={"filename": filename, "type": type},
        caller_agent_id=created_by,
    )
    now = _now()
    return {
        "id": result["row_id"],
        "project_id": project_id,
        "type": type,
        "title": title,
        "filename": filename,
        "created_by": created_by,
        "created_at": result.get("created_at", now),
        "updated_at": result.get("updated_at", now),
        "index_id": result.get("index_id"),
    }


def _row_to_legacy_dict(row: dict) -> dict:
    """Project a v1.1.0 `project_documents` row back to the v1.0.13 shape
    expected by existing callers (and test_documents.py). The legacy
    `type` value is reconstructed from `domain` / `subdomain` so that
    code paths consuming `doc["type"]` keep working.
    """
    out = dict(row)
    # Reconstruct legacy `type`: domain wins unless we have a subdomain
    # under project_doc, in which case the subdomain *is* the type
    # (e.g. ``("project_doc", "plan") -> type="plan"``).
    domain = out.get("domain")
    subdomain = out.get("subdomain")
    if domain == "project_doc" and subdomain:
        out["type"] = subdomain
    elif domain:
        out["type"] = domain
    return out


def get_document(db_path: str, doc_id: int) -> dict | None:
    """Return the project_documents row for `doc_id`, or None.

    Routes through `backend.get_document(doc_id)` (landed by
    atelier#52) and converts the v1.1.0 row shape to the legacy
    `type` field via `_row_to_legacy_dict` for backward-compat with
    pre-v1.1.0 callers.
    """
    del db_path
    row = backend.get_document(doc_id=doc_id)
    if row is None:
        return None
    return _row_to_legacy_dict(row)


def _decode_metadata(row: dict) -> dict:
    """Decode a document row's `metadata` field into a dict.

    Local mode stores `project_documents.metadata` as a JSON TEXT column
    (migration 007); Memex mode returns it folded as a dict already. This
    normalizes both: a JSON string is parsed, an already-dict value is
    returned as-is, None / missing / unparseable yields an empty dict (so
    callers can always `.get("version")` without a type check).
    """
    raw = row.get("metadata")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def write_spec_amendment(
    db_path: str,
    prior_doc_id: int,
    title: str,
    body: str,
    created_by: str,
) -> dict:
    """Amend a spec document by creating a NEW versioned row that supersedes it.

    Spec versioning (atelier#62, design spec §6.4). An amendment NEVER mutates
    the prior row in place — it creates a fresh `project_documents` row carrying
    `metadata = {"version": prior_version + 1, "supersedes": prior_doc_id}`, so
    the full version chain stays auditable and the superseded spec remains
    readable via `get_document(prior_doc_id)`.

    Contract:
      (a) Fetches the prior doc via `get_document`. Raises `ValueError` if it
          does not exist (amending a phantom is a caller error).
      (b) Refuses non-spec docs: a 9-section PM spec is stored as
          `domain=project, subdomain=spec` (§6.4). If the prior doc's
          (domain, subdomain) is not that pair we raise `ValueError` rather
          than silently version an arbitrary document — this is the "clean way
          to tell" the contract asks for.
      (c) Creates a NEW row via the backend facade (`backend.write_document`,
          never a direct `backend_local` call — CLAUDE.md routing rule) with
          the bumped version metadata. `prior_version` is read from the prior
          doc's metadata, defaulting to 1 when absent (a pre-#62 spec with no
          metadata is treated as version 1, so its first amendment is v2).
      (d) Returns the new row dict, including its decoded `metadata`.

    The new row inherits the prior doc's `workspace_id` / `project_id` /
    `domain` / `subdomain` so it lands in the same scope and stays a spec.
    """
    prior = get_document(db_path, prior_doc_id)
    if prior is None:
        raise ValueError(
            f"write_spec_amendment: prior_doc_id={prior_doc_id} not found — "
            "cannot amend a document that does not exist."
        )
    if prior.get("domain") != _SPEC_DOMAIN or prior.get("subdomain") != _SPEC_SUBDOMAIN:
        raise ValueError(
            f"write_spec_amendment: doc {prior_doc_id} is not a spec "
            f"(domain={prior.get('domain')!r}, subdomain={prior.get('subdomain')!r}); "
            f"only docs stored as domain={_SPEC_DOMAIN!r}, subdomain={_SPEC_SUBDOMAIN!r} "
            "(the §6.4 9-section spec coordinates) can be amended."
        )
    prior_meta = _decode_metadata(prior)
    prior_version = prior_meta.get("version")
    if not isinstance(prior_version, int) or prior_version < 1:
        prior_version = 1
    new_metadata = {"version": prior_version + 1, "supersedes": prior_doc_id}
    result = backend.write_document(
        workspace_id=prior.get("workspace_id"),
        project_id=prior.get("project_id"),
        domain=_SPEC_DOMAIN,
        subdomain=_SPEC_SUBDOMAIN,
        title=title,
        body=body,
        metadata=new_metadata,
        caller_agent_id=created_by,
    )
    # Re-read through get_document so the returned dict is the persisted row
    # (with metadata round-tripped through storage), not the write echo.
    new_id = result["row_id"]
    new_row = get_document(db_path, new_id)
    if new_row is None:
        # Defensive: the write succeeded (row_id returned) but the read-back
        # missed — surface the echo with decoded metadata rather than None.
        result["metadata"] = new_metadata
        return result
    new_row["metadata"] = _decode_metadata(new_row)
    return new_row


def update_document(db_path: str, doc_id: int, **kwargs) -> dict | None:
    """Patch-update `title`, `filename`, or legacy `type` on a document row.

    Unknown kwargs are silently dropped (matches the v1.0.13 contract).
    The legacy ``type`` kwarg is re-mapped through `TYPE_TO_DOMAIN` and
    written to `domain` / `subdomain`.

    Passing both `type` and `subdomain` is ambiguous: the `type` mapping
    determines a `subdomain` too, so an explicit caller-supplied
    `subdomain` would silently lose to whichever wins. Raise
    `ValueError` so the caller picks one or the other.
    """
    if kwargs.get("type") is not None and kwargs.get("subdomain") is not None:
        raise ValueError(
            "update_document: pass either `type` or `subdomain`, not both — "
            "`type` already determines a subdomain via TYPE_TO_DOMAIN."
        )
    allowed_direct = {"title", "filename", "subdomain"}
    changes: dict[str, object] = {
        k: v for k, v in kwargs.items() if k in allowed_direct and v is not None
    }
    if kwargs.get("type") is not None:
        domain, subdomain = _translate_type(kwargs["type"])
        changes["domain"] = domain
        changes["subdomain"] = subdomain
    changes["updated_at"] = _now()
    from scripts import mode_detector

    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        backend_memex._memex_core_update(
            store="atelier",
            table="project_documents",
            row_id=doc_id,
            changes=changes,
        )
    else:
        from scripts import backend_local

        now = changes.get("updated_at") or _now()
        c = backend_local._conn()
        try:
            if "title" in changes:
                c.execute(
                    "UPDATE project_documents SET title = ?, updated_at = ? WHERE id = ?",
                    (changes["title"], now, doc_id),
                )
            if "filename" in changes:
                c.execute(
                    "UPDATE project_documents SET filename = ?, updated_at = ? WHERE id = ?",
                    (changes["filename"], now, doc_id),
                )
            if "domain" in changes:
                c.execute(
                    "UPDATE project_documents SET domain = ?, updated_at = ? WHERE id = ?",
                    (changes["domain"], now, doc_id),
                )
            if "subdomain" in changes:
                c.execute(
                    "UPDATE project_documents SET subdomain = ?, updated_at = ? WHERE id = ?",
                    (changes["subdomain"], now, doc_id),
                )
            c.commit()
        finally:
            c.close()
    return get_document(db_path, doc_id)


def delete_document(db_path: str, doc_id: int) -> bool:
    """Delete the document row. Returns True iff a row was removed."""
    from scripts import mode_detector

    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        backend_memex._memex_core_delete(store="atelier", table="project_documents", row_id=doc_id)
        return True
    from scripts import backend_local

    c = backend_local._conn()
    try:
        cur = c.execute("DELETE FROM project_documents WHERE id = ?", (doc_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def list_documents(
    db_path: str, project_id: int | None = None, type: str | None = None
) -> list[dict]:
    """List project_documents rows, optionally filtered by `project_id`
    and/or legacy `type` (translated to `domain`).

    Sort order matches v1.0.13 (`ORDER BY title`).
    """
    from scripts import mode_detector

    domain_filter: str | None = None
    if type is not None:
        domain_filter, _ = _translate_type(type)
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        where: dict[str, object] = {}
        if project_id is not None:
            where["project_id"] = project_id
        if domain_filter is not None:
            where["domain"] = domain_filter
        rows = backend_memex._memex_core_query(
            store="atelier", table="project_documents", where=where
        )
    else:
        from scripts import backend_local

        conds: list[str] = []
        params: list[object] = []
        if project_id is not None:
            conds.append("project_id = ?")
            params.append(project_id)
        if domain_filter is not None:
            conds.append("domain = ?")
            params.append(domain_filter)
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        c = backend_local._conn()
        try:
            sql = f"SELECT * FROM project_documents {clause} ORDER BY title"  # nosec B608
            raw = c.execute(sql, params).fetchall()
        finally:
            c.close()
        rows = [dict(r) for r in raw]
    return [_row_to_legacy_dict(r) for r in rows]


def search_documents(db_path: str, query: str, project_id: int | None = None) -> list[dict]:
    """Full-text search over project_documents (FTS5 in Local mode,
    Memex Index search in Memex mode). Returns the v1.0.13-shaped dicts.

    For parity with v1.0.13's `LIKE '%query%'` semantics, a bare-word
    query is rewritten to an FTS5 prefix query (``OAuth -> OAuth*``) so
    "OAuth" still finds "OAuth2 Design". Callers that want literal token
    matching can pass an explicit FTS5 expression (anything containing
    whitespace, quotes, ``*``, or boolean operators is passed through
    unchanged — see `_maybe_prefix`).
    """
    q = _maybe_prefix(query.strip() if query else "")
    rows = backend.find_documents(query=q, project_id=project_id)
    return [_row_to_legacy_dict(r) for r in rows]


if __name__ == "__main__":
    import argparse
    import json
    import sys

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        print(
            json.dumps(
                create_document(
                    db_path,
                    project_id=int(sys.argv[2]),
                    type=sys.argv[3],
                    title=sys.argv[4],
                    filename=sys.argv[5],
                    created_by=sys.argv[6],
                ),
                indent=2,
            )
        )
    elif cmd == "get":
        result = get_document(db_path, int(sys.argv[2]))
        print(json.dumps(result, indent=2) if result else "Not found")
    elif cmd == "update":
        parser = argparse.ArgumentParser()
        parser.add_argument("doc_id", type=int)
        parser.add_argument("--title")
        parser.add_argument("--type")
        parser.add_argument("--filename")
        args = parser.parse_args(sys.argv[2:])
        kwargs = {k: v for k, v in vars(args).items() if k != "doc_id" and v is not None}
        print(json.dumps(update_document(db_path, args.doc_id, **kwargs), indent=2))
    elif cmd == "delete":
        print("Deleted" if delete_document(db_path, int(sys.argv[2])) else "Not found")
    elif cmd == "list":
        parser = argparse.ArgumentParser()
        parser.add_argument("--project_id", type=int)
        parser.add_argument("--type")
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(
                list_documents(db_path, project_id=args.project_id, type=args.type), indent=2
            )
        )
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--project_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(
            json.dumps(search_documents(db_path, args.query, project_id=args.project_id), indent=2)
        )
