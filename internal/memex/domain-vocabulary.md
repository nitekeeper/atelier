---
description: Engineer-facing reference for Atelier's two-level taxonomy (DOMAINS + SUBDOMAINS) and the v1 → v1.1 type-mapping policy. Read this before adding a domain or subdomain.
---

# Atelier domain vocabulary (Memex Index)

Atelier-side rows in `~/.memex/index.db.documents.domain` use this fixed
vocabulary when written via Tier 2 (caller-built `librarian_output`).
Memex does not enforce a domain enum — **this list IS the enforcement**,
maintained by Atelier and validated by
`scripts.domain_vocabulary.assert_valid()`.

The constants live in [`scripts/domain_vocabulary.py`](../../scripts/domain_vocabulary.py)
and are pinned by [`tests/test_domain_vocabulary.py`](../../tests/test_domain_vocabulary.py).

## Hard vs soft validation

| Constant | Validation | Where it lives | Why |
|---|---|---|---|
| `DOMAINS` (frozenset of 9) | **HARD** — `assert_valid()` raises `ValueError` on unknown | `~/.memex/index.db.documents.domain` (cross-plugin) | Memex Brain federates queries against this column; drift breaks cross-plugin search. |
| `SUBDOMAINS` (dict of lists) | **SOFT** — no `assert_valid_subdomain`; lookups accept any string | Atelier SQL only (`tasks.subdomain`, etc.) | Subdomain proliferation doesn't pollute Memex's namespace. A future audit can promote stable additions. |

If you find yourself wanting an `assert_valid_subdomain` helper: don't.
The soft-validation contract is deliberate per spec §6.4 line 398.

## Current vocabulary (9 domains, spec §6.4)

| Domain | Atelier source table | Promotion rationale |
|---|---|---|
| `project`     | `projects`                              | Top-level work efforts; cross-project recall ("what projects have I run"). |
| `task`        | `tasks`                                 | Atomic work items; cross-project recall ("what bugs did I fix last quarter"). |
| `meeting`     | `meeting_minutes`                       | Decisions/discussions cross-cut projects. |
| `design`      | `project_documents` (subset)            | Patterns recur across projects — "every auth design I've drafted". |
| `adr`         | `project_documents` (subset)            | High-value cross-project lookup is the canonical ADR use case. |
| `research`    | `project_documents` (subset)            | Tech-topic recall ("notes on Postgres tuning across all projects"). |
| `postmortem`  | `project_documents` (subset)            | Lessons cross-cut by failure mode, not project. |
| `log`         | `project_documents` (subset, or workspace-level) | Time-bounded recall; often workspace- or human-scoped. |
| `project_doc` | `project_documents` (catch-all)         | Generic bucket for typed-but-not-promoted docs (e.g., `plan`, `runbook`). |

`plan` deliberately does NOT get its own domain — plans are
project-bound and rarely useful cross-project. They ride under
`project_doc` with `subdomain="plan"`.

## Subdomain vocabulary (Atelier-internal, soft-validated)

| Domain | Stable subdomains |
|---|---|
| `task`        | `bug`, `chore`, `feature`, `refactor`, `spike` |
| `meeting`     | `1-1`, `customer`, `design-review`, `incident`, `kickoff`, `planning`, `retro`, `standup` |
| `design`      | `api`, `data`, `infra`, `migration`, `security`, `ux` |
| `research`    | `comparison`, `evaluation`, `reference`, `summary` |
| `postmortem`  | `incident`, `release`, `retro` |
| `log`         | `daily`, `decision`, `lesson` |
| `project_doc` | `plan`, `pr-description`, `release-notes`, `runbook`, free-form |
| `project`, `adr` | (no subdomains — atomic) |

Subdomain enforcement is **soft** — unknown values are accepted; the
list above documents the canonical set per domain. Drift is acceptable;
a future audit can promote stable additions.

## Legacy type → (domain, subdomain) mapping

`scripts.domain_vocabulary.TYPE_TO_DOMAIN` translates v1.0.13's
free-form `project_documents.type` strings into the v1.1.0 two-level
taxonomy. Consumed by `scripts/migrate_to_memex.py`'s legacy reader
(Plan 4). Unknown v1 types fall back to `("project_doc", <type>)` at
the call site per spec §11.4:

```python
domain, subdomain = TYPE_TO_DOMAIN.get(
    r["type"], ("project_doc", r["type"])
)
```

| v1 `type` string | v1.1.0 `(domain, subdomain)` |
|---|---|
| `design`          | `("design", None)` |
| `adr`             | `("adr", None)` |
| `research`        | `("research", None)` |
| `postmortem`      | `("postmortem", None)` |
| `log`             | `("log", None)` |
| `plan`            | `("project_doc", "plan")` |
| `runbook`         | `("project_doc", "runbook")` |
| `release-notes`   | `("project_doc", "release-notes")` |
| `pr-description`  | `("project_doc", "pr-description")` |
| `notes`           | `("project_doc", None)` |
| `spec`            | `("design", None)` — historical alias |
| *(anything else)* | `("project_doc", <type>)` — caller fallback |

## Addition policy

- **Adding a domain** — spec amendment; update `DOMAINS` frozenset; add
  test coverage in `tests/test_domain_vocabulary.py`. Bump
  `.claude-plugin/plugin.json`'s `version`.
- **Adding a subdomain** — Atelier-internal; update
  `SUBDOMAINS[domain]` list; PR comment justifying the addition.
  No spec change required.

The friction on domains exists because cross-plugin search relies on
stable strings. Adding a domain that overlaps with Memex Brain's own
taxonomy (`article`, `capture`, `synthesis`) would muddle
`memex:brain:ask` results. Worth the spec round-trip.

## Invariants worth knowing

These are enforced by `tests/test_domain_vocabulary.py` and by sister
constants Plan 2 will introduce:

- Every key in `SUBDOMAINS` is in `DOMAINS`.
- Every `(domain, _)` in `TYPE_TO_DOMAIN.values()` has `domain in DOMAINS`.
- Plan 2's `_DOMAIN_TO_TABLE` map (in `scripts/backend_memex.py`) will
  share the same invariant — every key in `_DOMAIN_TO_TABLE` is in
  `DOMAINS`.
- `project` and `adr` intentionally have no entry in `SUBDOMAINS` —
  they are atomic. Tests pin this.

## Cross-plugin invariant (Plan 2)

Plan 2's `_DOMAIN_TO_TABLE` map (in `scripts/backend_memex.py`) MUST share the same key-set invariant as `SUBDOMAINS`: every key is in `DOMAINS`. A sibling test `test_invariant_domain_to_table_keys_subset_of_domains` will land in Plan 2 to enforce this.
