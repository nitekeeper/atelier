# tests/test_regression_document_write_workspaceless_memex_mode.py
"""Iron-Law regression for atelier#90 part-3 — workspace-less Memex write.

BEFORE atelier#90 part-3 the facade `backend.write_document` RAISED
`NotImplementedError` whenever `workspace_id is None` in Memex mode (the
§6.7 `_no-workspace_` key construction was unimplemented). That gate
blocked the abort-report (a genuinely workspace-less postmortem doc) from
ever persisting in Memex mode (see test_abort.py AC#2).

AFTER part-3 the workspace-less Memex write lands via the §6.7 reserved
`_no-workspace_/(no-project)/<domain>/...` key: the facade threads an
EXPLICIT `workspace_less` discriminator (NOT metadata-absence — that
would hijack every legacy not-workspace-threaded write into the
no-workspace namespace) through
`backend.write_document -> backend_memex.write_document -> _atelier_write
-> _build_key`, where `workspace_part = workspace_slug or '_no-workspace_'`
mirrors the existing `project_part = project_slug or '(no-project)'`.

This file exercises the GENUINE workspace-less branch: it OVERRIDES the
autouse `_stub_singleton_workspace` (conftest.py:33) with a function-scope
monkeypatch that raises if the singleton fallback is ever reached, so a
regression that silently falls back to the singleton workspace (the
data-routing hazard called out in the design's Global risks) is caught.
"""

from __future__ import annotations

import pytest

from scripts import backend, backend_memex, mode_detector


def _force_memex(monkeypatch) -> None:
    """Force the facade router into Memex mode without touching Memex Core."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)


def _canonical_memex_stubs(monkeypatch, captured: dict) -> None:
    """The canonical hermetic Memex stub set (per
    test_regression_document_write_memex_mode.py:72-79 +
    test_workspace_slug_removal.py:245-264), capturing librarian_output so
    the §6.7 key is assertable. _next_seq is stubbed so the index.documents
    scan (which needs ~/.memex/config.json) never runs."""
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": "x",
                "key": k["librarian_output"]["key"],
                "domain": k["librarian_output"]["domain"],
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)


def _ban_singleton_fallback(monkeypatch) -> None:
    """OVERRIDE the autouse `_stub_singleton_workspace`: in the genuine
    workspace-less branch the singleton fallback must NEVER be reached. A
    function-scope monkeypatch applied AFTER the conftest autouse fixture
    wins (pytest applies test-scope overrides last). If a regression routes
    a both-None write through the singleton fallback (the data-routing
    hazard), these raise loudly instead of silently mislabeling the key."""

    def _boom_singleton():  # pragma: no cover - only fires on regression
        raise AssertionError(
            "workspace-less write reached _singleton_workspace fallback — "
            "the explicit workspace_less discriminator was not threaded"
        )

    def _boom_slug(_workspace_id):  # pragma: no cover - only fires on regression
        raise AssertionError(
            "workspace-less write reached _workspace_slug_for_id — "
            "it should never derive a slug when workspace_id is None"
        )

    monkeypatch.setattr(backend_memex, "_singleton_workspace", _boom_singleton)
    monkeypatch.setattr(backend_memex, "_workspace_slug_for_id", _boom_slug)


def test_workspaceless_memex_write_uses_no_workspace_key(monkeypatch):
    """In forced-Memex mode, `write_document(workspace_id=None,
    project_id=None, domain='postmortem')`:

      * BEFORE the fix RAISES NotImplementedError (the §6.7 gate);
      * AFTER the fix returns an echo dict (does NOT raise) and the
        captured §6.7 key starts with
        `_no-workspace_/(no-project)/postmortem/`.

    The singleton fallback is BANNED for this branch (see
    `_ban_singleton_fallback`) so the test proves the genuine
    workspace-less path, not an accidental singleton mislabel."""
    _force_memex(monkeypatch)
    _ban_singleton_fallback(monkeypatch)
    captured: dict = {}
    _canonical_memex_stubs(monkeypatch, captured)

    echo = backend.write_document(
        workspace_id=None,
        project_id=None,
        domain="postmortem",
        subdomain="abort",
        title="Team abort (hard): run-x",
        body="postmortem body",
        metadata={},
        caller_agent_id="abort",
    )

    # Did NOT raise: returns the backend echo dict.
    assert isinstance(echo, dict)
    key = captured["librarian_output"]["key"]
    assert key.startswith("_no-workspace_/(no-project)/postmortem/"), (
        f"workspace-less Memex key did not use the §6.7 reserved literal: {key!r}"
    )


def test_workspaceless_memex_write_leaves_workspace_id_null(monkeypatch):
    """The both-None write must NOT plant `workspace_id` in the payload or
    the metadata blob: `_workspace_id_for_project(None)` (which would raise
    ValueError) is never called, and `payload['workspace_id']` lands NULL
    (legal post-005). Guards the derivation-skip in
    backend_memex.write_document + the metadata-absence in _atelier_write."""
    _force_memex(monkeypatch)
    _ban_singleton_fallback(monkeypatch)
    captured: dict = {}
    _canonical_memex_stubs(monkeypatch, captured)
    # If the both-None path ever derived workspace_id from a project, this
    # would be hit with None and raise ValueError — guard it explicitly.
    monkeypatch.setattr(
        backend_memex,
        "_workspace_id_for_project",
        lambda pid: pytest.fail(
            f"_workspace_id_for_project called for workspace-less write (pid={pid!r})"
        ),
    )

    backend.write_document(
        workspace_id=None,
        project_id=None,
        domain="postmortem",
        subdomain="abort",
        title="Team abort (soft): run-y",
        body="body",
        metadata={},
        caller_agent_id="abort",
    )

    payload = captured["payload"]
    assert payload["workspace_id"] is None
    assert payload["project_id"] is None
    # The metadata blob must not carry an explicit workspace_id (absence,
    # not None) so the §10 _auto_relations filter reads it as 'any'.
    assert "workspace_id" not in captured["librarian_output"]["metadata"]


def test_mixed_workspace_none_project_real_is_not_workspace_less(monkeypatch):
    """MIXED call: ``write_document(workspace_id=None, project_id=<real>)``
    in Memex mode is NOT workspace-less. The facade discriminator is
    project-aware, so:

      * it does NOT raise (the §6.7 key is consistent with the payload),
      * the backend DERIVES the project's workspace_id into the payload,
      * the §6.7 key starts with the project's REAL workspace slug — NOT the
        reserved ``_no-workspace_`` literal.

    NON-VACUITY: under the OLD discriminator (``workspace_id`` absent in
    adapted_metadata ALONE, ignoring project_id) this same call would have
    set ``workspace_less=True`` and emitted a ``_no-workspace_`` key while the
    payload column carried the derived real id — an internally inconsistent
    row. We pin BOTH: the old predicate WOULD have flagged it workspace-less
    (so the project-aware AND-clause is load-bearing), and the new key uses
    the project's real workspace slug.
    """
    _force_memex(monkeypatch)
    captured: dict = {}
    _canonical_memex_stubs(monkeypatch, captured)
    # The mixed call derives workspace_id from the project; stub that lookup
    # and the project-slug resolution so the §6.7 key is assertable without a
    # real Memex registry. The singleton fallback stays available (autouse
    # conftest stub → slug "atelier") because for a single-workspace
    # deployment the project's workspace IS the singleton.
    monkeypatch.setattr(backend_memex, "_workspace_id_for_project", lambda pid: 1)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "auth")

    PROJECT_ID = 7
    # NON-VACUITY: the OLD discriminator (metadata-absence alone) WOULD have
    # flagged this mixed call workspace-less. Reproduce the facade's adapted
    # metadata fold to prove the project_id-aware AND-clause is what flips it.
    adapted_metadata: dict = {"project_id": PROJECT_ID}  # workspace_id=None skipped
    old_discriminator = "workspace_id" not in adapted_metadata
    assert old_discriminator is True, (
        "precondition: the old metadata-absence-only discriminator would "
        "have mislabeled this mixed call workspace-less"
    )

    echo = backend.write_document(
        workspace_id=None,
        project_id=PROJECT_ID,
        domain="design",
        subdomain="auth",
        title="Mixed Doc",
        body="body",
        metadata={},
        caller_agent_id="dr-samuel-okafor",
    )

    # Did NOT raise: returns the backend echo dict.
    assert isinstance(echo, dict)
    # The backend derived the project's workspace_id onto the payload.
    assert captured["payload"]["workspace_id"] == 1
    assert captured["payload"]["project_id"] == PROJECT_ID
    # The §6.7 key uses the project's REAL workspace slug, NOT _no-workspace_.
    key = captured["librarian_output"]["key"]
    assert not key.startswith("_no-workspace_"), (
        f"mixed call mislabeled workspace-less — key used the reserved "
        f"_no-workspace_ literal despite a derivable project workspace: {key!r}"
    )
    assert key.startswith("atelier/auth/design/"), (
        f"mixed-call key did not use the project's workspace/project slugs: {key!r}"
    )
