"""Contract tests for the persistence facade.

Originally Wave 0 — every method raised NotImplementedError; this file
asserted the signature + the raise. Plan 2 Task 9 wired 14 of the 20
methods through to `backend_memex` / `backend_local`; this file now
distinguishes:

  * IMPLEMENTED methods (14): no longer raise NotImplementedError. We
    confirm the signature still accepts the spec §4.3 kwargs (positional
    rejection still tested) and that the call DOES dispatch (mocking
    detect_mode → local + the underlying backend symbol).
  * DEFERRED methods (6): `find_or_create_workspace`,
    `find_workspace_by_identity`, `list_workspaces`, `find_project`,
    `list_projects`, `get_document` — still raise NotImplementedError
    per Plan 2 Task 9's "defer to v1.2.0" decision (spec §4.3 keeps them
    on the surface so callers don't feature-flag).

The full surface mirrors spec §4.3 lines 187-223.
"""

# Red-phase verified 2026-05-17: removing scripts/backend.py and running this
# file yields `ModuleNotFoundError: No module named 'scripts.backend'`.
# Restored via commit 8ff2504.
from unittest.mock import create_autospec, patch

import pytest

from scripts import backend, backend_local, mode_detector

EXPECTED_METHODS = [
    # Document-shaped writes — Tier 2
    "write_project",
    "write_document",
    "write_task",
    "write_meeting",
    # Operational state — Tier 1
    "upsert_session",
    "transition_phase",
    "update_task_status",
    "record_phase_bypass",
    # Workspace + project resolution
    "find_or_create_workspace",
    "find_workspace_by_identity",
    "list_workspaces",
    "find_project",
    "list_projects",
    # Reads
    "find_documents",
    "get_task",
    "list_tasks",
    "get_document",
    "lookup_index_id_by_source_ref",
    # Idempotent role/agent helpers — used by scripts/seed_roles.py
    # (Plan 3) and the bootstrap path. Both must be safe to call on a
    # populated DB.
    "find_or_create_role",
    "find_or_create_agent",
]


# Single source of truth for both `test_all_methods_defined` and
# `test_method_raises_not_implemented`. Each value is a dict of kwargs
# that satisfy the signature. Multiple rows per method exercise optional
# kwargs so signature drift is caught.
METHOD_KWARGS: dict[str, list[dict]] = {
    "write_project": [
        {"workspace_id": 1, "slug": "proj", "name": "Proj", "description": "d", "created_by": "a"},
    ],
    "write_document": [
        # Required-only.
        {
            "workspace_id": 1,
            "project_id": 1,
            "domain": "design",
            "subdomain": None,
            "title": "t",
            "body": "b",
            "metadata": {},
            "caller_agent_id": "a",
        },
        # All optionals exercised: source_url + relations.
        {
            "workspace_id": 1,
            "project_id": 1,
            "domain": "design",
            "subdomain": "x",
            "title": "t",
            "body": "b",
            "metadata": {"k": "v"},
            "caller_agent_id": "a",
            "source_url": "https://example.test/doc",
            "relations": [{"target_id": 1, "kind": "part_of"}],
        },
    ],
    "write_task": [
        # Required-only.
        {
            "workspace_id": 1,
            "project_id": 1,
            "title": "t",
            "description": "d",
            "subdomain": None,
            "created_by": "a",
        },
        # All optionals: assigned_to, priority, notes, relations.
        {
            "workspace_id": 1,
            "project_id": 1,
            "title": "t",
            "description": "d",
            "subdomain": "x",
            "created_by": "a",
            "assigned_to": "b",
            "priority": 5,
            "notes": "n",
            "relations": [{"target_id": 2, "kind": "blocks"}],
        },
    ],
    "write_meeting": [
        # Required-only.
        {
            "workspace_id": 1,
            "project_id": 1,
            "title": "t",
            "date": "2026-05-16",
            "summary": "s",
            "decisions": "d",
            "subdomain": None,
            "created_by": "a",
        },
        # All optionals: relations.
        {
            "workspace_id": 1,
            "project_id": 1,
            "title": "t",
            "date": "2026-05-16",
            "summary": "s",
            "decisions": "d",
            "subdomain": "x",
            "created_by": "a",
            "relations": [{"target_id": 3, "kind": "part_of"}],
        },
    ],
    "upsert_session": [
        # Required-only.
        {"project_id": 1, "agent_id": "a"},
        # All optionals: phase, current_tasks, accomplished, next_action,
        # status, pm_notes.
        {
            "project_id": 1,
            "agent_id": "a",
            "phase": "design:open",
            "current_tasks": "t1",
            "accomplished": "acc",
            "next_action": "next",
            "status": "done",
            "pm_notes": "n",
        },
    ],
    "transition_phase": [
        # Required-only.
        {"project_id": 1, "to_phase": "plan:open", "agent_id": "a"},
        # Optional bypass_reason.
        {"project_id": 1, "to_phase": "plan:open", "agent_id": "a", "bypass_reason": "urgent fix"},
    ],
    "update_task_status": [
        # Required-only.
        {"task_id": 1, "status": "in-progress"},
        # Optional notes.
        {"task_id": 1, "status": "done", "notes": "shipped"},
    ],
    "record_phase_bypass": [
        {"project_id": 1, "from_phase": "x", "to_phase": "y", "reason": "r", "agent_id": "a"},
    ],
    "find_or_create_workspace": [
        # Required-only.
        {"identity": "repo:x", "slug": "x", "name": "X"},
        # Optional description.
        {"identity": "repo:x", "slug": "x", "name": "X", "description": "first-create description"},
    ],
    "find_workspace_by_identity": [
        {"identity": "repo:x"},
    ],
    "list_workspaces": [
        {},
    ],
    "find_project": [
        {"workspace_id": 1, "slug": "proj"},
    ],
    "list_projects": [
        {"workspace_id": 1},
    ],
    "find_documents": [
        # Required-only.
        {"query": "q"},
        # All optionals: workspace_id, project_id, domain, subdomain, limit.
        {
            "query": "q",
            "workspace_id": 1,
            "project_id": 1,
            "domain": "design",
            "subdomain": "x",
            "limit": 25,
        },
    ],
    "get_task": [
        {"task_id": 1},
    ],
    "list_tasks": [
        # Required-only.
        {"project_id": 1},
        # Optional status.
        {"project_id": 1, "status": "done"},
    ],
    "get_document": [
        {"doc_id": 1},
    ],
    "lookup_index_id_by_source_ref": [
        {"source_ref": "atelier:tasks:1"},
    ],
    "find_or_create_role": [
        {"name": "Product Manager", "description": "PM"},
    ],
    "find_or_create_agent": [
        {"agent_id": "atelier-pm-1", "name": "PM", "role_id": 1, "profile": "pm"},
    ],
}


# ── Implemented vs deferred split (Plan 2 Task 9; atelier#51 + #52) ────────
#
# Originally Plan 2 Task 9 deferred 6 methods to v1.2.0. atelier#51 wired
# the workspace-layer trio (find_or_create_workspace,
# find_workspace_by_identity, list_workspaces). atelier#52 wired the
# project + document trio (find_project, list_projects, get_document).
# All 20 spec §4.3 methods now dispatch through a real backend; no
# `_not_implemented` stubs remain on the facade.
DEFERRED_METHODS: set[str] = set()

IMPLEMENTED_METHODS = set(EXPECTED_METHODS) - DEFERRED_METHODS


def test_all_methods_defined():
    """Every expected method is present on `backend`, and METHOD_KWARGS
    covers exactly the same set (no drift between the two lists)."""
    assert set(METHOD_KWARGS.keys()) == set(EXPECTED_METHODS), (
        "EXPECTED_METHODS and METHOD_KWARGS keys must match"
    )
    for name in EXPECTED_METHODS:
        assert hasattr(backend, name), f"backend.{name} missing"


# Flatten (name, kwargs) pairs into pytest.param rows with stable IDs so
# failures read like `[write_document-1]` rather than the default
# `[kwargs0]`. One ID per call site keeps multi-row methods unambiguous.
def _params_for(method_set):
    return [
        pytest.param(name, kw, id=name if len(METHOD_KWARGS[name]) == 1 else f"{name}-{i}")
        for name in EXPECTED_METHODS
        if name in method_set
        for i, kw in enumerate(METHOD_KWARGS[name])
    ]


_DEFERRED_PARAMS = _params_for(DEFERRED_METHODS)
_IMPLEMENTED_PARAMS = _params_for(IMPLEMENTED_METHODS)


@pytest.mark.parametrize("fn_name,kwargs", _DEFERRED_PARAMS)
def test_deferred_method_raises_not_implemented(fn_name, kwargs):
    """The 6 methods Plan 2 Task 9 deferred to v1.2.0 still raise
    NotImplementedError. Spec §4.3 keeps them on the surface."""
    fn = getattr(backend, fn_name)
    with pytest.raises(NotImplementedError):
        fn(**kwargs)


@pytest.mark.parametrize("fn_name,kwargs", _IMPLEMENTED_PARAMS)
def test_implemented_method_dispatches(fn_name, kwargs):
    """The 14 spec §4.3 methods Plan 2 Task 9 wired through must NOT raise
    NotImplementedError AND must actually invoke the backend symbol.

    Upgraded post-T16 (QA I1): the original test only asserted "no
    NotImplementedError", which a method returning `None` would silently
    pass. We now patch the facade's `_backend()` to return a
    `create_autospec(backend_local)` mock — autospec ensures the call
    signature matches the real backend symbol's signature, so signature
    drift between facade and backend gets caught here too — and assert
    the matching attribute was called once. The result value itself
    isn't pinned (stays a dispatch test, not a result test)."""
    mock_backend = create_autospec(backend_local, spec_set=True)
    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch.object(backend, "_backend", return_value=mock_backend),
    ):
        fn = getattr(backend, fn_name)
        fn(**kwargs)
    # Assert the matching backend symbol was invoked exactly once.
    # `create_autospec` already enforces signature compatibility on the
    # call, so a kwarg-mismatch fails before we reach this line.
    called = getattr(mock_backend, fn_name)
    called.assert_called_once()


# Methods with no required parameters can't be tested for positional
# rejection (there are no positional args to pass). Exclude them so the
# TypeError check below only runs against methods that take >=1 arg.
_METHODS_WITH_ARGS = [name for name in EXPECTED_METHODS if name != "list_workspaces"]


@pytest.mark.parametrize("fn_name", _METHODS_WITH_ARGS)
def test_methods_reject_positional_args(fn_name):
    """Wave 1 / 1' will swap implementations; keyword-only signatures
    prevent positional-arg drift between backends. Calling each method
    with positional args must raise TypeError."""
    fn = getattr(backend, fn_name)
    kwargs = METHOD_KWARGS[fn_name][0]
    positional_args = list(kwargs.values())
    with pytest.raises(TypeError):
        fn(*positional_args)
