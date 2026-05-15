---
name: doc
description: Use to create, read, update, delete, list, or search documents attached to Atelier projects.
---

# doc

Manages documents attached to projects — design docs, implementation plans, test reports, etc.

## Commands

- `doc:create` — Attach a document to a project
- `doc:read` — Get document details
- `doc:update` — Update document metadata
- `doc:delete` — Remove a document record
- `doc:list` — List documents (filter by project or type)
- `doc:search` — Search documents by title or type

## Procedure

### doc:create
1. Ask: "Project ID?" / "Document type (e.g. design, implementation-plan, test-report, security-report, qa-report)?" / "Title?" / "Filename (path to the markdown file)?" / "Your agent ID?"
2. Run: `python atelier/scripts/documents.py create <project_id> "<type>" "<title>" "<filename>" "<created_by>"`
3. Confirm: "Document registered: [title] (id: [id])"

### doc:read
1. Ask: "Document ID?"
2. Run: `python atelier/scripts/documents.py get <id>`
3. Display all fields.

### doc:update
1. Ask: "Document ID?" and "What to update?"
2. Run: `python atelier/scripts/documents.py update <id> [--title "..."] [--type "..."] [--filename "..."]`
3. Confirm: "Document updated."

### doc:delete
1. Ask: "Document ID? Note: this removes the DB record only, not the file."
2. Run: `python atelier/scripts/documents.py delete <id>`
3. Confirm: "Document record removed."

### doc:list
1. Ask: "Filter by project ID? Filter by type? (both optional)"
2. Run: `python atelier/scripts/documents.py list [--project_id N] [--type "<type>"]`
3. Display results as a table: id | project_id | type | title | filename

### doc:search
1. Ask: "Search query?" and "Filter by project ID? (optional)"
2. Run: `python atelier/scripts/documents.py search "<query>" [--project_id N]`
3. Display matching documents as a table.
