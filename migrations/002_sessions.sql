-- migrations/002_sessions.sql
-- PM working memory: one row per session close, prunable.

CREATE TABLE IF NOT EXISTS sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL REFERENCES projects(id),
    agent_id            TEXT NOT NULL REFERENCES agents(id),
    phase               TEXT,
    pre_diagnose_phase  TEXT,
    current_tasks       TEXT,
    accomplished        TEXT,
    next_action         TEXT,
    status              TEXT NOT NULL DEFAULT 'in-progress'
                            CHECK(status IN ('in-progress', 'blocked', 'complete')),
    blocking_reason     TEXT,
    pm_notes            TEXT,
    opened_at           TEXT,
    closed_at           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
