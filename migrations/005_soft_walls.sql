-- migrations/005_soft_walls.sql
-- Soft walls: add phase_bypasses table for logging out-of-phase skill invocations.
-- Spec: docs/superpowers/specs/2026-05-14-atelier-auto-trigger-and-soft-walls-design.md §3.3

CREATE TABLE IF NOT EXISTS phase_bypasses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    skill           TEXT NOT NULL,
    current_phase   TEXT NOT NULL,
    required_phase  TEXT NOT NULL,
    bypassed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    agent_id        TEXT REFERENCES agents(id),
    note            TEXT
);

CREATE INDEX IF NOT EXISTS phase_bypasses_project_idx ON phase_bypasses(project_id);
