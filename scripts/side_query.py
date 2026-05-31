# scripts/side_query.py
"""Human → worker side-query recording (atelier#64 AI-3; design §9.4).

In agent team mode the "human only talks to PM" rule is softly relaxed: the
human MAY directly side-query a worker via the worker's dedicated tmux pane for
quick clarifications / observation. Two hard invariants from §9.4:

* A side-query does NOT redirect the worker — its dispatched task / role is
  unchanged. This module only RECORDS the exchange; it never mutates the
  worker's assignment, never enqueues a dispatch, never touches the task table.
* A side-query does NOT replace PM-mediated escalation. The two are independent
  paths; recording a side-query neither raises nor suppresses any PM escalation.

## Canonical store vs durable-backend mirror

* **Canonical: ``team_audit_log``** — an ``event_type='side_query'`` row whose
  payload JSON is ``{prompt, response, worker_role_id}``. PM retains full
  context by reading this ledger. This write is the source of truth and MUST
  succeed for ``record_side_query`` to succeed.
* **Mirror: durable backend (§9.4)** — BEST-EFFORT
  ``backend.write_document(domain='log', subdomain='side-query', …)``. A mirror
  failure MUST NOT fail the side-query and MUST NOT drop the canonical audit
  row — the mirror is convenience indexing, not the record of truth. The mirror
  carries the SAME prompt + response + worker_role_id (AC3).
"""

from __future__ import annotations

from scripts import backend

SIDE_QUERY_EVENT_TYPE = "side_query"


def record_side_query(
    *,
    team_id: str,
    worker_role_id: str,
    prompt: str,
    response: str,
    project_id: int | None = None,
    workspace_id: int | None = None,
    mirror: bool = True,
) -> dict:
    """Record a human→worker side-query.

    Writes the canonical ``team_audit_log`` ``event_type='side_query'`` row
    FIRST (source of truth), then — best-effort — mirrors it to the durable
    backend (``domain='log', subdomain='side-query'``). Returns
    ``{"audit": <row>, "mirrored": bool, "mirror_error": str | None}``.

    By contract this function ONLY records. It does not redirect the worker
    (no task/role mutation) and does not invoke PM escalation — those are
    independent paths (§9.4).
    """
    payload = {
        "prompt": prompt,
        "response": response,
        "worker_role_id": worker_role_id,
    }
    # Canonical write — must succeed; any failure propagates (no swallow).
    audit_row = backend.write_team_audit(
        team_id=team_id,
        event_type=SIDE_QUERY_EVENT_TYPE,
        payload=payload,
    )

    mirrored = False
    mirror_error: str | None = None
    if mirror:
        try:
            backend.write_document(
                workspace_id=workspace_id,
                project_id=project_id,
                domain="log",
                subdomain="side-query",
                title=f"side-query: {worker_role_id}",
                body=_render_mirror_body(prompt, response),
                metadata={
                    "team_id": team_id,
                    "worker_role_id": worker_role_id,
                    # Same prompt + response + role_id in the mirror (AC3).
                    "prompt": prompt,
                    "response": response,
                },
                caller_agent_id="human-side-query",
            )
            mirrored = True
        except Exception as e:  # mirror is best-effort by design (§9.4)
            # §9.4: a mirror failure must NOT fail the side-query nor drop the
            # canonical audit row (already written above). Surface the reason
            # for observability, but never re-raise.
            mirror_error = f"{type(e).__name__}: {e}"

    return {"audit": audit_row, "mirrored": mirrored, "mirror_error": mirror_error}


def _render_mirror_body(prompt: str, response: str) -> str:
    """Render the durable-backend mirror body. The prompt + response are
    quoted as DATA (the untrusted-input boundary): a side-query prompt is
    human-authored text under study, never instructions to atelier's runtime."""
    return f"## Prompt\n\n{prompt}\n\n## Response\n\n{response}\n"
