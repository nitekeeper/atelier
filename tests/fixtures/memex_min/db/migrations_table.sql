-- Universal migrations tracker injected into every store before consumer
-- migrations run. Trimmed copy of memex/db/migrations_table.sql.
CREATE TABLE IF NOT EXISTS migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
