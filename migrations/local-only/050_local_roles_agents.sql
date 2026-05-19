-- migrations/local-only/050_local_roles_agents.sql
-- Local mode owns its own roles + agents tables; Memex mode defers to
-- ~/.memex/agents.db (spec §6.5). Schema mirrors what agents.db exposes
-- so business logic does not branch on mode.

CREATE TABLE roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role_id    INTEGER NOT NULL REFERENCES roles(id),
    profile    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
