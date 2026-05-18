"""Tests for scripts.domain_vocabulary — Atelier two-level taxonomy.

Pins the contract from spec §6.4 (two-level taxonomy) and §11.4 (legacy
reader). DOMAINS is hard-validated (frozenset, ValueError on unknown);
SUBDOMAINS is soft-validated (documentation only; lookups accept any
string). TYPE_TO_DOMAIN translates v1.0.13's free-form
`project_documents.type` column to v1.1.0's `(domain, subdomain)` pair.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import domain_vocabulary as dv


# ── Public surface ──────────────────────────────────────────────────────


def test_public_surface_locked():
    """`__all__` pins the public surface so Plan 2 sibling modules can't
    accidentally expand it."""
    assert dv.__all__ == (
        "DOMAINS",
        "SUBDOMAINS",
        "TYPE_TO_DOMAIN",
        "assert_valid",
        "assert_valid_domain",
    )


# ── DOMAINS ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "domain",
    [
        "project",
        "task",
        "meeting",
        "design",
        "adr",
        "research",
        "postmortem",
        "log",
        "project_doc",
    ],
    ids=[
        "project",
        "task",
        "meeting",
        "design",
        "adr",
        "research",
        "postmortem",
        "log",
        "project_doc",
    ],
)
def test_canonical_domains_present(domain):
    """Spec §6.4 lines 369-381: the nine canonical domains."""
    assert domain in dv.DOMAINS


def test_domains_is_frozenset_of_nine():
    """Frozenset enforces immutability; nine is the spec count."""
    assert isinstance(dv.DOMAINS, frozenset)
    assert len(dv.DOMAINS) == 9, (
        f"expected 9 domains per spec §6.4, got {len(dv.DOMAINS)}"
    )


def test_domains_are_strings():
    for d in dv.DOMAINS:
        assert isinstance(d, str)
        assert d  # non-empty


def test_assert_valid_accepts_every_domain():
    for d in dv.DOMAINS:
        dv.assert_valid(d)  # must not raise


def test_assert_valid_rejects_unknown():
    with pytest.raises(ValueError, match="unknown domain"):
        dv.assert_valid("blog_post")


def test_assert_valid_error_lists_valid_domains():
    """The error message must include the full sorted list of valid
    domains so debugging is fast. The impl renders via
    ``f"{sorted(DOMAINS)}"`` so every domain appears as ``repr(d)``."""
    with pytest.raises(ValueError) as excinfo:
        dv.assert_valid("not_a_real_domain")
    msg = str(excinfo.value)
    for d in dv.DOMAINS:
        assert repr(d) in msg, (
            f"error message missing domain {d!r}: {msg}"
        )


def test_assert_valid_rejects_empty_string():
    with pytest.raises(ValueError, match="unknown domain"):
        dv.assert_valid("")


def test_assert_valid_raises_typeerror_on_none():
    with pytest.raises(TypeError, match="domain must be str"):
        dv.assert_valid(None)  # type: ignore[arg-type]


def test_assert_valid_raises_typeerror_on_int():
    with pytest.raises(TypeError, match="domain must be str"):
        dv.assert_valid(42)  # type: ignore[arg-type]


def test_assert_valid_is_case_sensitive():
    with pytest.raises(ValueError):
        dv.assert_valid("Project")


def test_assert_valid_rejects_whitespace_padded():
    with pytest.raises(ValueError):
        dv.assert_valid(" project ")


def test_assert_valid_domain_alias_is_same_function():
    """The explicit-name alias points to the same function object."""
    assert dv.assert_valid_domain is dv.assert_valid


# ── SUBDOMAINS ──────────────────────────────────────────────────────────


def test_subdomains_is_dict_keyed_by_domain():
    """Structural test: keys are domains. Implies the keys-subset-of-
    DOMAINS invariant — no separate invariant test needed."""
    # MappingProxyType is a Mapping, not a dict. Test the interface.
    assert hasattr(dv.SUBDOMAINS, "__getitem__")
    assert hasattr(dv.SUBDOMAINS, "items")
    for d in dv.SUBDOMAINS:
        assert d in dv.DOMAINS, (
            f"SUBDOMAINS references unknown domain {d!r}"
        )


def test_subdomains_is_immutable():
    """SUBDOMAINS is MappingProxyType — assignment raises TypeError."""
    with pytest.raises(TypeError):
        dv.SUBDOMAINS["task"] = ()  # type: ignore[index]


def test_subdomains_values_are_tuples():
    for d, subs in dv.SUBDOMAINS.items():
        assert isinstance(subs, tuple), (
            f"SUBDOMAINS[{d!r}] should be tuple, got {type(subs).__name__}"
        )


def test_subdomains_values_are_sorted():
    """Stylistic invariant: each subdomain tuple is sorted alphabetically."""
    for domain, subs in dv.SUBDOMAINS.items():
        assert list(subs) == sorted(subs), (
            f"{domain} subdomains not sorted: {subs}"
        )


def test_subdomains_cover_documented_domains():
    """Spec §6.4 lines 385-396 lists subdomains for 7 of the 9 domains
    (`project` and `adr` are atomic — no subdomains)."""
    for d in (
        "task",
        "meeting",
        "design",
        "research",
        "postmortem",
        "log",
        "project_doc",
    ):
        assert d in dv.SUBDOMAINS, f"{d} missing subdomain catalog"
        assert isinstance(dv.SUBDOMAINS[d], tuple)
        assert len(dv.SUBDOMAINS[d]) > 0


def test_subdomains_skip_atomic_domains():
    """`project` and `adr` are atomic per spec §6.4 line 396 — they
    intentionally have no subdomain catalog entry."""
    assert "project" not in dv.SUBDOMAINS
    assert "adr" not in dv.SUBDOMAINS


def test_subdomain_specific_canonical_values():
    """Spot-check canonical entries from spec §6.4 lines 385-396."""
    assert "standup" in dv.SUBDOMAINS["meeting"]
    assert "bug" in dv.SUBDOMAINS["task"]
    assert "api" in dv.SUBDOMAINS["design"]
    assert "plan" in dv.SUBDOMAINS["project_doc"]


def test_subdomain_lists_are_nonempty_strings():
    """Every SUBDOMAINS[domain] entry is a non-empty string."""
    for d, subs in dv.SUBDOMAINS.items():
        for s in subs:
            assert isinstance(s, str) and s, (
                f"SUBDOMAINS[{d!r}] has non-string or empty entry {s!r}"
            )


def test_subdomain_is_soft_validated_no_assert():
    """Soft validation contract: there is intentionally NO
    `assert_valid_subdomain` helper. The SUBDOMAINS map is
    documentation; drift is acceptable per spec §6.4 line 398."""
    assert not hasattr(dv, "assert_valid_subdomain"), (
        "subdomain validation is soft; do not introduce assert_valid_subdomain"
    )


# ── TYPE_TO_DOMAIN ──────────────────────────────────────────────────────


def test_type_to_domain_is_immutable():
    """TYPE_TO_DOMAIN is MappingProxyType — assignment raises TypeError."""
    with pytest.raises(TypeError):
        dv.TYPE_TO_DOMAIN["foo"] = ("project_doc", None)  # type: ignore[index]


def test_type_to_domain_returns_domain_subdomain_pairs():
    for v1_type, mapped in dv.TYPE_TO_DOMAIN.items():
        assert isinstance(mapped, tuple) and len(mapped) == 2, (
            f"TYPE_TO_DOMAIN[{v1_type!r}] is not a 2-tuple"
        )
        domain, subdomain = mapped
        assert domain in dv.DOMAINS, (
            f"TYPE_TO_DOMAIN[{v1_type!r}] domain {domain!r} not in DOMAINS"
        )
        assert subdomain is None or isinstance(subdomain, str)


@pytest.mark.parametrize(
    "v1_type",
    ["design", "plan", "adr", "research", "postmortem"],
    ids=["design", "plan", "adr", "research", "postmortem"],
)
def test_type_to_domain_covers_known_v1_types(v1_type):
    """The mapping must handle v1.0.13's stable type values. Unknown
    values fall back to ('project_doc', <type>) at the call site, per
    spec §11.4."""
    assert v1_type in dv.TYPE_TO_DOMAIN, (
        f"v1 type {v1_type!r} missing from TYPE_TO_DOMAIN"
    )


@pytest.mark.parametrize(
    "v1_type,expected_domain",
    [
        ("design", "design"),
        ("adr", "adr"),
        ("research", "research"),
        ("postmortem", "postmortem"),
        ("log", "log"),
    ],
    ids=["design", "adr", "research", "postmortem", "log"],
)
def test_type_to_domain_promotion_targets(v1_type, expected_domain):
    """Spec §6.4 promotes a subset of v1 types to first-class domains.
    Each promoted type maps to its own domain with subdomain=None."""
    assert dv.TYPE_TO_DOMAIN[v1_type] == (expected_domain, None), (
        f"{v1_type!r} should map to ({expected_domain!r}, None)"
    )


@pytest.mark.parametrize(
    "v1_type,expected",
    [
        ("design", ("design", None)),
        ("adr", ("adr", None)),
        ("research", ("research", None)),
        ("postmortem", ("postmortem", None)),
        ("log", ("log", None)),
        ("plan", ("project_doc", "plan")),
        ("runbook", ("project_doc", "runbook")),
        ("release-notes", ("project_doc", "release-notes")),
        ("pr-description", ("project_doc", "pr-description")),
        ("notes", ("project_doc", None)),
        ("spec", ("design", None)),  # historical alias
    ],
    ids=[
        "design",
        "adr",
        "research",
        "postmortem",
        "log",
        "plan",
        "runbook",
        "release-notes",
        "pr-description",
        "notes",
        "spec",
    ],
)
def test_type_to_domain_full_mapping(v1_type, expected):
    """Pins every documented v1 type → (domain, subdomain) pair from
    spec §11.4."""
    assert dv.TYPE_TO_DOMAIN[v1_type] == expected


# ── Invariants ──────────────────────────────────────────────────────────


def test_invariant_all_type_to_domain_first_elements_in_domains():
    """Every value in TYPE_TO_DOMAIN has its (domain, ...) first element
    in DOMAINS — guards against typo drift."""
    for v1_type, (domain, _sub) in dv.TYPE_TO_DOMAIN.items():
        assert domain in dv.DOMAINS, (
            f"TYPE_TO_DOMAIN[{v1_type!r}] domain {domain!r} not in DOMAINS"
        )


# ── Doc ─────────────────────────────────────────────────────────────────


def test_vocabulary_doc_exists():
    f = (
        Path(__file__).parent.parent
        / "internal"
        / "memex"
        / "domain-vocabulary.md"
    )
    assert f.exists(), f"missing {f}"
    text = f.read_text(encoding="utf-8")
    for d in (
        "project",
        "task",
        "meeting",
        "design",
        "adr",
        "research",
        "postmortem",
        "log",
        "project_doc",
    ):
        assert d in text, f"doc missing domain {d!r}"


def test_vocabulary_doc_mentions_addition_policy():
    """Engineer-facing doc must surface the hard-vs-soft validation
    policy and the addition workflow (spec amendment for domains,
    PR-comment for subdomains)."""
    f = (
        Path(__file__).parent.parent
        / "internal"
        / "memex"
        / "domain-vocabulary.md"
    )
    text = f.read_text(encoding="utf-8").lower()
    assert "addition policy" in text or "adding a domain" in text
    assert "subdomain" in text
