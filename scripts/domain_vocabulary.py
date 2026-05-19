"""Atelier's domain vocabulary for Tier 2 writes through Memex Index.

Memex doesn't enforce a domain enum — Atelier owns the small, stable set.
Adding a new domain is a deliberate spec revision, not an inline call.
See ``internal/memex/domain-vocabulary.md`` for the policy.

Three constants live here:

- ``DOMAINS``        — frozenset of 9 strings; cross-plugin enforcement.
                       HARD-validated via :func:`assert_valid`.
- ``SUBDOMAINS``     — MappingProxyType ``{domain: (stable subdomains,)}``;
                       Atelier-internal, SOFT-validated (unknown values
                       are accepted; this map is documentation, not a gate).
- ``TYPE_TO_DOMAIN`` — MappingProxyType ``{v1_type_string: (domain, subdomain)}``;
                       consumed by ``scripts/migrate_to_memex.py``'s
                       legacy reader (Plan 4). Unknown v1 types fall back
                       to ``("project_doc", <type>)`` at the call site.

Spec references:
- §6.4  — two-level taxonomy (domain + subdomain).
- §11.4 — legacy reader pseudocode using ``TYPE_TO_DOMAIN.get(...)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


__all__ = (
    "DOMAINS",
    "SUBDOMAINS",
    "TYPE_TO_DOMAIN",
    "assert_valid",
    "assert_valid_domain",
)


# ── DOMAINS (spec §6.4, lines 369-381) ──────────────────────────────────
#
# Cross-plugin, stable. Written to ``~/.memex/index.db.documents.domain``
# on every Tier 2 write. Adding a new domain requires a spec amendment;
# the friction is intentional — domains that overlap Memex Brain's own
# taxonomy (e.g. ``article``, ``capture``, ``synthesis``) would muddle
# federated ``memex:brain:ask`` results.

DOMAINS: frozenset[str] = frozenset(
    {
        "project",  # atelier.db.projects rows
        "task",  # atelier.db.tasks rows
        "meeting",  # atelier.db.meeting_minutes rows
        "design",  # project_documents subset — system/feature designs
        "adr",  # project_documents subset — architecture decision records
        "research",  # project_documents subset — reference + evaluation notes
        "postmortem",  # project_documents subset — incident/release/retro write-ups
        "log",  # project_documents subset (or workspace-level) — journals
        "project_doc",  # project_documents catch-all (plans, runbooks, release notes)
    }
)


# ── SUBDOMAINS (spec §6.4, lines 385-396) ───────────────────────────────
#
# Atelier-internal, lightweight policy. Stored in Atelier-side columns
# (``tasks.subdomain``, ``meeting_minutes.subdomain``,
# ``project_documents.subdomain``). NEVER written to Memex Index, so
# subdomain proliferation doesn't pollute the cross-plugin namespace.
#
# Soft validation: there is intentionally NO ``assert_valid_subdomain``.
# Callers may pass any string; the tuples below document the canonical
# set per domain. A future audit can roll up frequencies and promote
# stable additions.
#
# Values are sorted tuples (immutable) wrapped in MappingProxyType to
# lock the public surface — mutation raises TypeError.
#
# ``project`` and ``adr`` are atomic — no subdomain catalog.

SUBDOMAINS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "task": ("bug", "chore", "feature", "refactor", "spike"),
        "meeting": (
            "1-1",
            "customer",
            "design-review",
            "incident",
            "kickoff",
            "planning",
            "retro",
            "standup",
        ),
        "design": ("api", "data", "infra", "migration", "security", "ux"),
        "research": ("comparison", "evaluation", "reference", "summary"),
        "postmortem": ("incident", "release", "retro"),
        "log": ("daily", "decision", "lesson"),
        "project_doc": ("plan", "pr-description", "release-notes", "runbook"),
        # "project" and "adr" intentionally omitted — atomic domains.
    }
)


# ── TYPE_TO_DOMAIN (spec §11.4) ─────────────────────────────────────────
#
# Translates v1.0.13's free-form ``project_documents.type`` column to
# v1.1.0's ``(domain, subdomain)`` pair. Consumed by Plan 4's
# ``scripts/migrate_to_memex.py`` legacy reader. Unknown v1 types fall
# back to ``("project_doc", <type>)`` at the call site, per spec §11.4
# line 1027::
#
#     domain, subdomain = TYPE_TO_DOMAIN.get(r["type"], ("project_doc", r["type"]))
#
# Two flavors of mapping:
#
# 1. Promoted to first-class domain — v1 type becomes the domain itself,
#    subdomain is ``None``.
# 2. Stays under ``project_doc`` with a meaningful subdomain.
#
# In TYPE_TO_DOMAIN, the second tuple element is the subdomain.
# ``None`` means "atomic — no subdomain"; do NOT substitute the v1 type.
# Missing keys fall back to ``("project_doc", <v1_type>)`` per spec §11.4.

TYPE_TO_DOMAIN: Mapping[str, tuple[str, str | None]] = MappingProxyType(
    {
        # ── Promoted to first-class domains in v1.1.0 ──
        "design": ("design", None),
        "adr": ("adr", None),
        "research": ("research", None),
        "postmortem": ("postmortem", None),
        "log": ("log", None),
        # ── Stay under project_doc with a meaningful subdomain ──
        "plan": ("project_doc", "plan"),
        "runbook": ("project_doc", "runbook"),
        "release-notes": ("project_doc", "release-notes"),
        "pr-description": ("project_doc", "pr-description"),
        "notes": ("project_doc", None),
        # ── Historical aliases ──
        "spec": ("design", None),  # v1 occasionally used "spec" for design docs
    }
)


def assert_valid(domain: str) -> None:
    """Hard-validate a domain string against the v1.1.0 vocabulary.

    Alias ``assert_valid_domain`` is also exported for clarity at call sites.

    Raises :class:`TypeError` if ``domain`` is not a string, and
    :class:`ValueError` with a precise diagnostic listing every valid
    domain in sorted order so debugging is fast.

    Subdomains are **not** validated here — see :data:`SUBDOMAINS` for
    the soft canonical set; callers may pass any string. Spec §6.4
    line 398 documents the rationale ("subdomain enforcement is soft").

    Args:
        domain: The candidate domain string.

    Raises:
        TypeError: If ``domain`` is not a :class:`str`.
        ValueError: If ``domain`` is not a member of :data:`DOMAINS`.
    """
    if not isinstance(domain, str):
        raise TypeError(f"domain must be str, got {type(domain).__name__}")
    if domain not in DOMAINS:
        raise ValueError(
            f"unknown domain {domain!r}; must be one of: "
            f"{sorted(DOMAINS)}. See internal/memex/domain-vocabulary.md"
        )


# Alias for explicit call sites. Both names point to the same function.
assert_valid_domain = assert_valid
