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


# ── DOMAINS ─────────────────────────────────────────────────────────────


def test_canonical_domains_present():
    """Spec §6.4 lines 369-379: the nine canonical domains."""
    expected = {
        "project",
        "task",
        "meeting",
        "design",
        "adr",
        "research",
        "postmortem",
        "log",
        "project_doc",
    }
    assert expected <= dv.DOMAINS


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
    domains so debugging is fast."""
    with pytest.raises(ValueError) as excinfo:
        dv.assert_valid("not_a_real_domain")
    msg = str(excinfo.value)
    # Every canonical domain appears in the message.
    for d in dv.DOMAINS:
        assert repr(d) in msg or d in msg, (
            f"error message missing domain {d!r}: {msg}"
        )


def test_assert_valid_rejects_empty_string():
    with pytest.raises(ValueError, match="unknown domain"):
        dv.assert_valid("")


# ── SUBDOMAINS ──────────────────────────────────────────────────────────


def test_subdomains_is_dict_keyed_by_domain():
    assert isinstance(dv.SUBDOMAINS, dict)
    for d in dv.SUBDOMAINS:
        assert d in dv.DOMAINS, (
            f"SUBDOMAINS references unknown domain {d!r}"
        )


def test_subdomains_cover_documented_domains():
    """Spec §6.4 lines 385-394 lists subdomains for 7 of the 9 domains
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
        assert isinstance(dv.SUBDOMAINS[d], (list, tuple, frozenset))
        assert len(dv.SUBDOMAINS[d]) > 0


def test_subdomains_skip_atomic_domains():
    """`project` and `adr` are atomic by spec §6.4 line 394 — they
    intentionally have no subdomain catalog entry."""
    assert "project" not in dv.SUBDOMAINS
    assert "adr" not in dv.SUBDOMAINS


def test_subdomain_specific_canonical_values():
    """Spot-check canonical entries from spec §6.4 lines 385-394."""
    assert "standup" in dv.SUBDOMAINS["meeting"]
    assert "bug" in dv.SUBDOMAINS["task"]
    assert "api" in dv.SUBDOMAINS["design"]
    assert "plan" in dv.SUBDOMAINS["project_doc"]


def test_subdomain_lists_sorted_or_documented():
    """Stylistic: every SUBDOMAINS[domain] is a list of strings. The
    plan doesn't require strict sorting, but each value must be a
    non-empty string."""
    for d, subs in dv.SUBDOMAINS.items():
        for s in subs:
            assert isinstance(s, str) and s, (
                f"SUBDOMAINS[{d!r}] has non-string or empty entry {s!r}"
            )


def test_subdomain_is_soft_validated_no_assert():
    """Soft validation contract: there is intentionally NO
    `assert_valid_subdomain` helper. The SUBDOMAINS dict is
    documentation; drift is acceptable per spec §6.4 line 396."""
    assert not hasattr(dv, "assert_valid_subdomain"), (
        "subdomain validation is soft; do not introduce assert_valid_subdomain"
    )


# ── TYPE_TO_DOMAIN ──────────────────────────────────────────────────────


def test_type_to_domain_is_dict():
    assert isinstance(dv.TYPE_TO_DOMAIN, dict)


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


def test_type_to_domain_covers_known_v1_types():
    """The mapping must handle v1.0.13's stable type values. Unknown
    values fall back to ('project_doc', <type>) at the call site, per
    spec §11.4 line 1027."""
    for v1_type in ("design", "plan", "adr", "research", "postmortem"):
        assert v1_type in dv.TYPE_TO_DOMAIN, (
            f"v1 type {v1_type!r} missing from TYPE_TO_DOMAIN"
        )


def test_type_to_domain_promotion_targets():
    """Spec §6.4 promotes a subset of v1 types to first-class domains.
    Each promoted type maps to its own domain with subdomain=None."""
    promoted = {
        "design": "design",
        "adr": "adr",
        "research": "research",
        "postmortem": "postmortem",
        "log": "log",
    }
    for v1_type, expected_domain in promoted.items():
        assert dv.TYPE_TO_DOMAIN[v1_type] == (expected_domain, None), (
            f"{v1_type!r} should map to ({expected_domain!r}, None)"
        )


def test_type_to_domain_project_doc_subdomains():
    """Non-promoted types ride under project_doc with a meaningful
    subdomain."""
    assert dv.TYPE_TO_DOMAIN["plan"] == ("project_doc", "plan")
    assert dv.TYPE_TO_DOMAIN["runbook"] == ("project_doc", "runbook")


# ── Invariants ──────────────────────────────────────────────────────────


def test_invariant_all_type_to_domain_first_elements_in_domains():
    """Every value in TYPE_TO_DOMAIN has its (domain, ...) first element
    in DOMAINS — guards against typo drift."""
    for v1_type, (domain, _sub) in dv.TYPE_TO_DOMAIN.items():
        assert domain in dv.DOMAINS, (
            f"TYPE_TO_DOMAIN[{v1_type!r}] domain {domain!r} not in DOMAINS"
        )


def test_invariant_subdomains_keys_subset_of_domains():
    """Every key in SUBDOMAINS is a known domain. Plan 2 will add a
    related `_DOMAIN_TO_TABLE` map with the same invariant (every key
    in `_DOMAIN_TO_TABLE` is in DOMAINS); documented here so the test
    review sees the parallel contract even though Plan 2 isn't here yet.
    """
    for d in dv.SUBDOMAINS:
        assert d in dv.DOMAINS


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
