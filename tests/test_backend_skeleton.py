"""Wave 0 contract tests for the persistence facade.

Each method must exist with the expected signature and raise
NotImplementedError when called. Wave 1 / Wave 1' replace the bodies.
The full surface mirrors spec §4.3 lines 187-223.
"""
# Red-phase verified 2026-05-17: removing scripts/backend.py and running this
# file yields `ModuleNotFoundError: No module named 'scripts.backend'`.
# Restored via commit 8ff2504.
import pytest

from scripts import backend


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
        dict(workspace_id=1, slug="proj", name="Proj",
             description="d", created_by="a"),
    ],
    "write_document": [
        # Required-only.
        dict(workspace_id=1, project_id=1, domain="design",
             subdomain=None, title="t", body="b",
             metadata={}, caller_agent_id="a"),
        # All optionals exercised: source_url + relations.
        dict(workspace_id=1, project_id=1, domain="design",
             subdomain="x", title="t", body="b",
             metadata={"k": "v"}, caller_agent_id="a",
             source_url="https://example.test/doc",
             relations=[{"target_id": 1, "kind": "part_of"}]),
    ],
    "write_task": [
        # Required-only.
        dict(workspace_id=1, project_id=1, title="t",
             description="d", subdomain=None, created_by="a"),
        # All optionals: assigned_to, priority, notes, relations.
        dict(workspace_id=1, project_id=1, title="t",
             description="d", subdomain="x", created_by="a",
             assigned_to="b", priority=5, notes="n",
             relations=[{"target_id": 2, "kind": "blocks"}]),
    ],
    "write_meeting": [
        # Required-only.
        dict(workspace_id=1, project_id=1, title="t",
             date="2026-05-16", summary="s", decisions="d",
             subdomain=None, created_by="a"),
        # All optionals: relations.
        dict(workspace_id=1, project_id=1, title="t",
             date="2026-05-16", summary="s", decisions="d",
             subdomain="x", created_by="a",
             relations=[{"target_id": 3, "kind": "part_of"}]),
    ],
    "upsert_session": [
        # Required-only.
        dict(project_id=1, agent_id="a"),
        # All optionals: phase, current_tasks, accomplished, next_action,
        # status, pm_notes.
        dict(project_id=1, agent_id="a", phase="design:open",
             current_tasks="t1", accomplished="acc",
             next_action="next", status="done", pm_notes="n"),
    ],
    "transition_phase": [
        # Required-only.
        dict(project_id=1, to_phase="plan:open", agent_id="a"),
        # Optional bypass_reason.
        dict(project_id=1, to_phase="plan:open", agent_id="a",
             bypass_reason="urgent fix"),
    ],
    "update_task_status": [
        # Required-only.
        dict(task_id=1, status="in-progress"),
        # Optional notes.
        dict(task_id=1, status="done", notes="shipped"),
    ],
    "record_phase_bypass": [
        dict(project_id=1, from_phase="x", to_phase="y",
             reason="r", agent_id="a"),
    ],
    "find_or_create_workspace": [
        # Required-only.
        dict(identity="repo:x", slug="x", name="X"),
        # Optional description.
        dict(identity="repo:x", slug="x", name="X",
             description="first-create description"),
    ],
    "find_workspace_by_identity": [
        dict(identity="repo:x"),
    ],
    "list_workspaces": [
        dict(),
    ],
    "find_project": [
        dict(workspace_id=1, slug="proj"),
    ],
    "list_projects": [
        dict(workspace_id=1),
    ],
    "find_documents": [
        # Required-only.
        dict(query="q"),
        # All optionals: workspace_id, project_id, domain, subdomain, limit.
        dict(query="q", workspace_id=1, project_id=1,
             domain="design", subdomain="x", limit=25),
    ],
    "get_task": [
        dict(task_id=1),
    ],
    "list_tasks": [
        # Required-only.
        dict(project_id=1),
        # Optional status.
        dict(project_id=1, status="done"),
    ],
    "get_document": [
        dict(doc_id=1),
    ],
    "lookup_index_id_by_source_ref": [
        dict(source_ref="atelier:tasks:1"),
    ],
    "find_or_create_role": [
        dict(name="Product Manager", description="PM"),
    ],
    "find_or_create_agent": [
        dict(agent_id="atelier-pm-1", name="PM", role_id=1, profile="pm"),
    ],
}


def test_all_methods_defined():
    """Every expected method is present on `backend`, and METHOD_KWARGS
    covers exactly the same set (no drift between the two lists)."""
    assert set(METHOD_KWARGS.keys()) == set(EXPECTED_METHODS), \
        "EXPECTED_METHODS and METHOD_KWARGS keys must match"
    for name in EXPECTED_METHODS:
        assert hasattr(backend, name), f"backend.{name} missing"


# Flatten (name, kwargs) pairs into pytest.param rows with stable IDs so
# failures read like `[write_document-1]` rather than the default
# `[kwargs0]`. One ID per call site keeps multi-row methods unambiguous.
_PARAMS = [
    pytest.param(name, kw,
                 id=name if len(kwlist) == 1 else f"{name}-{i}")
    for name, kwlist in METHOD_KWARGS.items()
    for i, kw in enumerate(kwlist)
]


@pytest.mark.parametrize("fn_name,kwargs", _PARAMS)
def test_method_raises_not_implemented(fn_name, kwargs):
    fn = getattr(backend, fn_name)
    with pytest.raises(NotImplementedError):
        fn(**kwargs)


# Methods with no required parameters can't be tested for positional
# rejection (there are no positional args to pass). Exclude them so the
# TypeError check below only runs against methods that take >=1 arg.
_METHODS_WITH_ARGS = [name for name in EXPECTED_METHODS
                     if name != "list_workspaces"]


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
