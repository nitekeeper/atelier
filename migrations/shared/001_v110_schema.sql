-- migrations/shared/001_v110_schema.sql
-- Atelier v1.1.0 schema. Clean redesign for Memex-v2 integration.
--
-- This is THE schema. There is no v1.0.13 -> v1.1.0 ALTER path; the
-- migration replay reads v1 rows via scripts.migrate_to_memex's legacy
-- reader and translates them into this layout (spec §11.4, Plan 4).
--
-- Both Memex-mode bootstrap (via memex:core:create-store) and Local-mode
-- setup consume this file. Local mode additionally consumes
-- migrations/local-only/050_local_roles_agents.sql.
--
-- Source of truth: docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md §11.2.

PRAGMA foreign_keys = ON;

------------------------------------------------------------------------
-- workspaces -- one row per repository on disk
------------------------------------------------------------------------
CREATE TABLE workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,            -- §0.2 slug(), used in keys per §6.7
    identity    TEXT UNIQUE NOT NULL,            -- repo_url if remote exists else realpath(git_root)
    name        TEXT NOT NULL,                   -- human-readable, original casing
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_workspaces_identity ON workspaces(identity);

------------------------------------------------------------------------
-- projects -- logical work efforts within a workspace
------------------------------------------------------------------------
CREATE TABLE projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    slug         TEXT NOT NULL,                  -- unique within (workspace_id, slug)
    name         TEXT NOT NULL,                  -- original casing
    description  TEXT,
    phase        TEXT NOT NULL DEFAULT 'design:open',
    created_by   TEXT NOT NULL,                  -- agents.id string; resolved via Memex agents.db or local agents
    index_id     TEXT,                           -- ~/.memex/index.db.documents.index_id backlink
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(workspace_id, slug)
);
CREATE INDEX idx_projects_workspace ON projects(workspace_id);
CREATE INDEX idx_projects_phase     ON projects(phase);
CREATE INDEX idx_projects_index_id  ON projects(index_id);
-- NOTE: v1.0.13's `repo` column is REMOVED -- workspaces.identity owns that fact.

------------------------------------------------------------------------
-- project_documents -- pointer-rows to markdown files on disk
------------------------------------------------------------------------
CREATE TABLE project_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),     -- denormalized for cross-project query
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    domain       TEXT NOT NULL,                  -- design / adr / research / postmortem / log / project_doc (per §6.4)
    subdomain    TEXT,                           -- plan / runbook / api / data / ... (soft-validated; §6.4)
    title        TEXT NOT NULL,                  -- original casing
    filename     TEXT NOT NULL,                  -- relative to workspace_root
    created_by   TEXT NOT NULL,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_docs_workspace  ON project_documents(workspace_id);
CREATE INDEX idx_docs_project    ON project_documents(project_id);
CREATE INDEX idx_docs_domain     ON project_documents(domain);
CREATE INDEX idx_docs_subdomain  ON project_documents(subdomain);
CREATE INDEX idx_docs_index_id   ON project_documents(index_id);
-- NOTE: v1.0.13's `type` column is REMOVED. The v1.0.13 type values
-- (design/plan/adr/research/...) are translated by the legacy reader
-- into (domain, subdomain) via scripts.domain_vocabulary.TYPE_TO_DOMAIN.

------------------------------------------------------------------------
-- tasks -- atomic work items within a project
------------------------------------------------------------------------
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    title        TEXT NOT NULL,
    description  TEXT,
    subdomain    TEXT,                           -- bug / feature / chore / spike / refactor (soft; §6.4)
    status       TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER DEFAULT 0,
    notes        TEXT,
    created_by   TEXT NOT NULL,
    assigned_to  TEXT,
    claimed_at   TEXT,
    completed_at TEXT,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_tasks_project    ON tasks(project_id);
CREATE INDEX idx_tasks_status     ON tasks(status);
CREATE INDEX idx_tasks_assigned   ON tasks(assigned_to);
CREATE INDEX idx_tasks_subdomain  ON tasks(subdomain);
CREATE INDEX idx_tasks_index_id   ON tasks(index_id);

------------------------------------------------------------------------
-- meeting_minutes -- meetings; may be workspace-level (no project)
------------------------------------------------------------------------
CREATE TABLE meeting_minutes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    project_id   INTEGER REFERENCES projects(id),                 -- nullable for workspace-level meetings
    title        TEXT NOT NULL,
    date         TEXT NOT NULL,                  -- YYYY-MM-DD per §0.2
    subdomain    TEXT,                           -- standup / design-review / retro / 1-1 / ... (soft; §6.4)
    filename     TEXT,                           -- nullable; relative to workspace_root if exported
    summary      TEXT,
    decisions    TEXT,
    created_by   TEXT NOT NULL,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_meetings_workspace ON meeting_minutes(workspace_id);
CREATE INDEX idx_meetings_project   ON meeting_minutes(project_id);
CREATE INDEX idx_meetings_date      ON meeting_minutes(date);
CREATE INDEX idx_meetings_subdomain ON meeting_minutes(subdomain);
CREATE INDEX idx_meetings_index_id  ON meeting_minutes(index_id);

------------------------------------------------------------------------
-- meeting_participants -- join table
------------------------------------------------------------------------
CREATE TABLE meeting_participants (
    meeting_id INTEGER NOT NULL REFERENCES meeting_minutes(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    PRIMARY KEY (meeting_id, agent_id)
);

------------------------------------------------------------------------
-- sessions -- PM working memory; one per work session, prunable (Tier 1 storage)
------------------------------------------------------------------------
CREATE TABLE sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id       INTEGER NOT NULL REFERENCES workspaces(id),
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    agent_id           TEXT NOT NULL,
    phase              TEXT,
    pre_diagnose_phase TEXT,
    current_tasks      TEXT,
    accomplished       TEXT,
    next_action        TEXT,
    status             TEXT NOT NULL DEFAULT 'in-progress'
                          CHECK(status IN ('in-progress', 'blocked', 'complete')),
    blocking_reason    TEXT,
    pm_notes           TEXT,
    opened_at          TEXT,
    closed_at          TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sessions_workspace ON sessions(workspace_id);
CREATE INDEX idx_sessions_project   ON sessions(project_id);
CREATE INDEX idx_sessions_agent     ON sessions(agent_id);
CREATE INDEX idx_sessions_status    ON sessions(status);

------------------------------------------------------------------------
-- Phase machine -- static catalog tables (identical to v1.0.13 except for inline seed)
------------------------------------------------------------------------
CREATE TABLE phases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    skill           TEXT NOT NULL,
    state           TEXT NOT NULL,
    description     TEXT NOT NULL,
    is_terminal     BOOLEAN NOT NULL DEFAULT FALSE,
    allow_from_any  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE phase_transitions (
    from_phase TEXT NOT NULL REFERENCES phases(name),
    to_phase   TEXT NOT NULL REFERENCES phases(name),
    PRIMARY KEY (from_phase, to_phase)
);

CREATE TABLE skill_gates (
    skill          TEXT PRIMARY KEY,
    required_phase TEXT REFERENCES phases(name)
    -- NULL means no gate: skill can be invoked from any phase
);

CREATE TABLE phase_bypasses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    from_phase  TEXT NOT NULL,
    to_phase    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_bypasses_project ON phase_bypasses(project_id);

-- ── Seed: 19 phases ────────────────────────────────────────────────────────
-- Verbatim from v1.0.13's 003_phases.sql so a fresh install gets the full
-- catalog in one file. v1.1.0 ships clean -- no separate phases migration.

INSERT OR IGNORE INTO phases (name, skill, state, description, is_terminal, allow_from_any) VALUES
    ('design:open',                'dev:design',   'open',              'Grilling and drafting in progress',         0, 0),
    ('design:approved',            'dev:design',   'approved',          'Design document approved by user',          0, 0),
    ('plan:open',                  'dev:plan',     'open',              'Implementation plan being written',         0, 0),
    ('plan:approved',              'dev:plan',     'approved',          'Plan approved, ready for TDD',              0, 0),
    ('tdd:red',                    'dev:tdd',      'red',               'Failing tests written',                     0, 0),
    ('tdd:green',                  'dev:tdd',      'green',             'Tests passing with minimal implementation', 0, 0),
    ('tdd:clean',                  'dev:tdd',      'clean',             'Code refactored, tests still passing',      0, 0),
    ('review:open',                'dev:review',   'open',              'Code review in progress',                   0, 0),
    ('review:changes-requested',   'dev:review',   'changes-requested', 'Reviewer requested changes',                0, 0),
    ('review:approved',            'dev:review',   'approved',          'Code review passed',                        0, 0),
    ('security:open',              'dev:security', 'open',              'Security review in progress',               0, 0),
    ('security:changes-requested', 'dev:security', 'changes-requested', 'Security issues raised',                   0, 0),
    ('security:approved',          'dev:security', 'approved',          'Security review passed',                    0, 0),
    ('qa:open',                    'dev:qa',       'open',              'QA review in progress',                     0, 0),
    ('qa:changes-requested',       'dev:qa',       'changes-requested', 'QA found blocking issues',                  0, 0),
    ('qa:approved',                'dev:qa',       'approved',          'QA passed, ready for handoff',              0, 0),
    ('diagnose:open',              'dev:diagnose', 'open',              'Bug diagnosis in progress',                 0, 1),
    ('diagnose:resolved',          'dev:diagnose', 'resolved',          'Bug diagnosed and fixed',                   0, 0),
    ('handoff:complete',           'dev:handoff',  'complete',          'Session closed, snapshot written',          1, 0);

-- ── Seed: phase transitions ────────────────────────────────────────────────

INSERT OR IGNORE INTO phase_transitions (from_phase, to_phase) VALUES
    -- Design
    ('design:open',              'design:approved'),
    ('design:approved',          'plan:open'),
    -- Plan
    ('plan:open',                'plan:approved'),
    ('plan:approved',            'tdd:red'),
    -- TDD cycle (tdd:clean → tdd:red allows repeat cycles)
    ('tdd:red',                  'tdd:green'),
    ('tdd:green',                'tdd:clean'),
    ('tdd:clean',                'tdd:red'),
    ('tdd:clean',                'review:open'),
    -- Review loop
    ('review:open',              'review:changes-requested'),
    ('review:changes-requested', 'review:open'),
    ('review:open',              'review:approved'),
    -- Security loop
    ('review:approved',                'security:open'),
    ('security:open',                  'security:changes-requested'),
    ('security:changes-requested',     'security:open'),
    ('security:open',                  'security:approved'),
    -- QA loop
    ('security:approved',        'qa:open'),
    ('qa:open',                  'qa:changes-requested'),
    ('qa:changes-requested',     'qa:open'),
    ('qa:open',                  'qa:approved'),
    -- Handoff
    ('qa:approved',              'handoff:complete'),
    -- diagnose cycle
    ('diagnose:open',            'diagnose:resolved'),
    -- diagnose:resolved can return to any pre-diagnose phase
    ('diagnose:resolved',        'design:open'),
    ('diagnose:resolved',        'design:approved'),
    ('diagnose:resolved',        'plan:open'),
    ('diagnose:resolved',        'plan:approved'),
    ('diagnose:resolved',        'tdd:red'),
    ('diagnose:resolved',        'tdd:green'),
    ('diagnose:resolved',        'tdd:clean'),
    ('diagnose:resolved',        'review:open'),
    ('diagnose:resolved',        'review:approved'),
    ('diagnose:resolved',        'security:open'),
    ('diagnose:resolved',        'security:approved'),
    ('diagnose:resolved',        'qa:open');

-- ── Seed: skill gates ──────────────────────────────────────────────────────

INSERT OR IGNORE INTO skill_gates (skill, required_phase) VALUES
    ('dev:design',   NULL),
    ('dev:plan',     'design:approved'),
    ('dev:tdd',      'plan:approved'),
    ('dev:review',   'tdd:clean'),
    ('dev:security', 'review:approved'),
    ('dev:qa',       'security:approved'),
    ('dev:diagnose', NULL),
    ('dev:handoff',  NULL);
