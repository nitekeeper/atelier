---
description: Internal — Local-mode raw-body archive helper. Content-addressed disk store under `<workspace>/.atelier/raw/`.
---

# local/wiki-archive (internal)

Helper recipe describing the raw-body archive used by
`internal/local/wiki-write`. You normally do not call this directly —
`backend_local.write_document` invokes it for you. Read this when
debugging missing/corrupt raw files or designing a recovery path.

## Layout

```
<workspace>/.atelier/raw/
├── 1a/
│   └── <canonical_key>.md      # e.g. auth-design-1a4b9c2f.md
├── 7d/
│   └── <canonical_key>.md
└── ...
```

Every archived body lives at:

```
<workspace>/.atelier/raw/<2char-hash-prefix>/<canonical_key>.md
```

Where:

- `<2char-hash-prefix>` = first 2 hex chars of `sha256(body)`. Spreads
  files across 256 directories so no single dir explodes past the FS
  inode soft-limit.
- `<canonical_key>` = `<slug(title)>-<first-8-hex-chars-of-sha256(body)>`.
  Slug is `[^a-z0-9]+` collapsed to `-`, truncated to 64 chars. The
  trailing 8-hex shard makes the filename collision-free even when two
  documents share a title.

## Recipe

`scripts.backend_local._archive_raw(body, title)` returns the **absolute
path** of the archived file. Semantics:

1. Compute `h = sha256(body).hexdigest()`.
2. Build `raw_dir = <workspace>/.atelier/raw/<h[:2]>/`. `mkdir -p` it.
3. Build `path = raw_dir / f"{slug(title)}-{h[:8]}.md"`.
4. If `path` already exists, **do nothing**. Idempotent on content hash.
5. Else write `body` (UTF-8) to `path`.
6. Return `str(path)`.

## Why this shape

- **Content-addressed.** Re-archiving the same bytes is a no-op, so
  retries are safe and dedup is automatic.
- **Title-readable.** The `<canonical_key>` carries the slug so a human
  `ls` over `.atelier/raw/` is browsable, not pure-hash soup.
- **Recoverable.** If `atelier.db` is lost, the raw archive is enough to
  rehydrate `documents` rows: `raw_path` is the primary record and
  `documents` is a searchable cache layered on top.

## Hard rules

- Never write to `.atelier/raw/` from anywhere other than `_archive_raw`.
  Out-of-band files break the dedup invariant and the `raw_path`
  back-reference.
- Never overwrite an existing path. Same bytes → identical file. Different
  bytes → different `<canonical_key>` (different `h[:8]`).
- The archive is **not** a backup. Operational state (sessions, tasks,
  phase_bypasses) lives only in `atelier.db`; back that up separately.
