-- agents.db: minimal role/agent registry for hermetic Atelier bootstrap tests.
-- Trimmed copy of memex/db/agents.sql; the trimmed columns aren't read by
-- bootstrap.py's seeders so the schema stays compatible with the full one.

CREATE TABLE IF NOT EXISTS roles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    description  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    role_id      INTEGER NOT NULL REFERENCES roles(id),
    profile      TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS agents_role_idx ON agents(role_id);
