-- migrations/001_initial_schema.sql
CREATE TABLE IF NOT EXISTS roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role_id     INTEGER NOT NULL REFERENCES roles(id),
    profile     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    repo        TEXT,
    phase       TEXT NOT NULL DEFAULT 'design:in-progress',
    created_by  TEXT NOT NULL REFERENCES agents(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    created_by  TEXT NOT NULL REFERENCES agents(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id),
    title       TEXT NOT NULL,
    description TEXT,
    created_by  TEXT NOT NULL REFERENCES agents(id),
    assigned_to TEXT REFERENCES agents(id),
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER DEFAULT 0,
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_minutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    date        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    summary     TEXT,
    decisions   TEXT,
    created_by  TEXT NOT NULL REFERENCES agents(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_participants (
    meeting_id  INTEGER NOT NULL REFERENCES meeting_minutes(id),
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    PRIMARY KEY (meeting_id, agent_id)
);

CREATE TABLE IF NOT EXISTS migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL
);
