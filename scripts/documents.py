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
from datetime import datetime, timezone
from pathlib import Path

from scripts import backend
from scripts.domain_vocabulary import TYPE_TO_DOMAIN


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


def create_document(db_path: str, project_id: int, type: str,
                    title: str, filename: str, created_by: str,
                    workspace_id: int | None = None) -> dict:
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
        out.setdefault("type", subdomain)
    elif domain:
        out.setdefault("type", domain)
    return out


def get_document(db_path: str, doc_id: int) -> dict | None:
    """Return the project_documents row for `doc_id`, or None.

    `backend.get_document` is deferred to v1.2.0 (spec §10), so we drop
    one layer down per mode and read the row directly.
    """
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        rows = backend_memex._memex_core_query(
            store="atelier", table="project_documents",
            where={"id": doc_id})
    else:
        from scripts import backend_local
        c = backend_local._conn()
        try:
            r = c.execute(
                "SELECT * FROM project_documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            c.close()
        rows = [dict(r)] if r else []
    if not rows:
        return None
    return _row_to_legacy_dict(rows[0])


def update_document(db_path: str, doc_id: int, **kwargs) -> dict | None:
    """Patch-update `title`, `filename`, or legacy `type` on a document row.

    Unknown kwargs are silently dropped (matches the v1.0.13 contract).
    The legacy ``type`` kwarg is re-mapped through `TYPE_TO_DOMAIN` and
    written to `domain` / `subdomain`.
    """
    allowed_direct = {"title", "filename", "subdomain"}
    changes: dict[str, object] = {
        k: v for k, v in kwargs.items() if k in allowed_direct and v is not None
    }
    if kwargs.get("type") is not None:
        domain, subdomain = _translate_type(kwargs["type"])
        changes["domain"] = domain
        # Only overwrite subdomain if the caller didn't pass one directly.
        changes.setdefault("subdomain", subdomain)
    changes["updated_at"] = _now()
    from scripts import mode_detector
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex
        backend_memex._memex_core_update(
            store="atelier", table="project_documents",
            row_id=doc_id, changes=changes,
        )
    else:
        from scripts import backend_local
        c = backend_local._conn()
        try:
            sets = ", ".join(f"{k} = ?" for k in changes)
            c.execute(
                f"UPDATE project_documents SET {sets} WHERE id = ?",
                (*changes.values(), doc_id),
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
        backend_memex._ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        # memex_stores.query() is SELECT-only and never commits — use the
        # dedicated `delete()` primitive so the row is actually removed.
        memex_stores.delete(name="atelier", table="project_documents",
                            row_id=doc_id)
        return True
    from scripts import backend_local
    c = backend_local._conn()
    try:
        cur = c.execute(
            "DELETE FROM project_documents WHERE id = ?", (doc_id,)
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def list_documents(db_path: str, project_id: int | None = None,
                   type: str | None = None) -> list[dict]:
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
            store="atelier", table="project_documents", where=where)
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
            sql = f"SELECT * FROM project_documents {clause} ORDER BY title"
            raw = c.execute(sql, params).fetchall()
        finally:
            c.close()
        rows = [dict(r) for r in raw]
    return [_row_to_legacy_dict(r) for r in rows]


def search_documents(db_path: str, query: str,
                     project_id: int | None = None) -> list[dict]:
    """Full-text search over project_documents (FTS5 in Local mode,
    Memex Index search in Memex mode). Returns the v1.0.13-shaped dicts.

    For parity with v1.0.13's `LIKE '%query%'` semantics, a bare-word
    query is rewritten to an FTS5 prefix query (``OAuth -> OAuth*``) so
    "OAuth" still finds "OAuth2 Design". Callers that want literal token
    matching can pass an explicit FTS5 expression (anything containing
    whitespace, quotes, ``*``, or boolean operators is passed through
    unchanged).
    """
    q = query.strip() if query else ""
    if q and not any(c in q for c in (" ", "\"", "*", ":", "(", ")")) \
            and q.upper() not in ("AND", "OR", "NOT"):
        q = f"{q}*"
    rows = backend.find_documents(query=q, project_id=project_id)
    return [_row_to_legacy_dict(r) for r in rows]


if __name__ == "__main__":
    import sys
    import json
    import argparse

    db_path = ".ai/memex.db"
    cmd = sys.argv[1]

    if cmd == "create":
        print(json.dumps(create_document(db_path, project_id=int(sys.argv[2]),
                                          type=sys.argv[3], title=sys.argv[4],
                                          filename=sys.argv[5], created_by=sys.argv[6]), indent=2))
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
        print(json.dumps(list_documents(db_path, project_id=args.project_id, type=args.type), indent=2))
    elif cmd == "search":
        parser = argparse.ArgumentParser()
        parser.add_argument("query")
        parser.add_argument("--project_id", type=int)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(search_documents(db_path, args.query, project_id=args.project_id), indent=2))
