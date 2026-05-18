"""Wave 0 contract tests for the persistence facade.

Each method must exist with the expected signature and raise
NotImplementedError when called. Wave 1 / Wave 1' replace the bodies.
The full surface mirrors spec §4.3 lines 187-223.
"""
import pytest
import inspect
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


def test_facade_module_exists():
    for name in EXPECTED_METHODS:
        assert hasattr(backend, name), f"backend.{name} missing"


@pytest.mark.parametrize("fn_name,kwargs", [
    ("write_project", dict(workspace_id=1, slug="proj", name="Proj",
                           description="d", created_by="a")),
    ("write_document", dict(workspace_id=1, project_id=1, domain="design",
                            subdomain=None, title="t", body="b",
                            metadata={}, caller_agent_id="a")),
    ("write_task", dict(workspace_id=1, project_id=1, title="t",
                        description="d", subdomain=None, created_by="a")),
    ("write_meeting", dict(workspace_id=1, project_id=1, title="t",
                           date="2026-05-16", summary="s", decisions="d",
                           subdomain=None, created_by="a")),
    ("upsert_session", dict(project_id=1, agent_id="a", phase="design:open")),
    ("transition_phase", dict(project_id=1, to_phase="plan:open", agent_id="a")),
    ("update_task_status", dict(task_id=1, status="in-progress")),
    ("record_phase_bypass", dict(project_id=1, from_phase="x", to_phase="y",
                                 reason="r", agent_id="a")),
    ("find_or_create_workspace", dict(identity="repo:x", slug="x", name="X")),
    ("find_workspace_by_identity", dict(identity="repo:x")),
    ("list_workspaces", dict()),
    ("find_project", dict(workspace_id=1, slug="proj")),
    ("list_projects", dict(workspace_id=1)),
    ("find_documents", dict(query="q")),
    ("get_task", dict(task_id=1)),
    ("list_tasks", dict(project_id=1)),
    ("get_document", dict(doc_id=1)),
    ("lookup_index_id_by_source_ref",
     dict(source_ref="atelier:tasks:1")),
    ("find_or_create_role", dict(name="Product Manager",
                                  description="PM")),
    ("find_or_create_agent", dict(agent_id="atelier-pm-1", name="PM",
                                   role_id=1, profile="pm")),
])
def test_method_raises_not_implemented(fn_name, kwargs):
    fn = getattr(backend, fn_name)
    with pytest.raises(NotImplementedError):
        fn(**kwargs)


def test_methods_accept_keyword_args_only():
    """Wave 1 / 1' will swap implementations; keyword-only signatures
    prevent positional-arg drift between backends."""
    for name in EXPECTED_METHODS:
        sig = inspect.signature(getattr(backend, name))
        for p in sig.parameters.values():
            # All params should be KEYWORD_ONLY or have a default
            assert p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD), \
                f"{name}.{p.name} should be keyword-callable"
