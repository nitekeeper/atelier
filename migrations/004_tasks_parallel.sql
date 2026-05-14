-- migrations/004_tasks_parallel.sql
-- Adds parallel_group TEXT to tasks.
-- Migrates priority from INTEGER to TEXT enum (critical/high/medium/low).
-- Recreates tasks table (SQLite cannot alter column type in place).

CREATE TABLE tasks_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    title           TEXT NOT NULL,
    description     TEXT,
    created_by      TEXT NOT NULL REFERENCES agents(id),
    assigned_to     TEXT REFERENCES agents(id),
    status          TEXT NOT NULL DEFAULT 'pending',
    priority        TEXT NOT NULL DEFAULT 'medium'
                        CHECK(priority IN ('critical', 'high', 'medium', 'low')),
    parallel_group  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Assumes priority is stored as INTEGER (0–3) in all existing rows.
-- CAST(TEXT AS INTEGER) returns 0 for non-numeric TEXT, which falls through to 'low'.
-- Safe for initial migration; not safe if TEXT priorities were ever written directly.
INSERT INTO tasks_new
    (id, project_id, title, description, created_by, assigned_to,
     status, priority, parallel_group, notes, created_at, updated_at)
SELECT
    id, project_id, title, description, created_by, assigned_to,
    status,
    CASE CAST(priority AS INTEGER)
        WHEN 3 THEN 'critical'
        WHEN 2 THEN 'high'
        WHEN 1 THEN 'medium'
        ELSE 'low'
    END,
    NULL,
    notes, created_at, updated_at
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
