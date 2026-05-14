-- migrations/003_phases.sql
-- Phase state machine: phases, transitions, skill gates.
-- Also renames existing projects.phase values to unified vocabulary
-- and updates the projects table DEFAULT.

-- ── Tables ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS phases (
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

CREATE TABLE IF NOT EXISTS phase_transitions (
    from_phase  TEXT NOT NULL REFERENCES phases(name),
    to_phase    TEXT NOT NULL REFERENCES phases(name),
    PRIMARY KEY (from_phase, to_phase)
);

CREATE TABLE IF NOT EXISTS skill_gates (
    skill           TEXT PRIMARY KEY,
    required_phase  TEXT REFERENCES phases(name)
    -- NULL means no gate: skill can be invoked from any phase
);

-- ── Seed: 19 phases ────────────────────────────────────────────────────────

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

-- ── Migrate existing projects to unified phase vocabulary ──────────────────

UPDATE projects SET phase = 'design:open'    WHERE phase = 'design:in-progress';
UPDATE projects SET phase = 'plan:open'      WHERE phase = 'plan:in-progress';
UPDATE projects SET phase = 'tdd:clean'      WHERE phase = 'tdd:refactor';
UPDATE projects SET phase = 'review:open'    WHERE phase = 'code-review:draft';
UPDATE projects SET phase = 'review:changes-requested' WHERE phase = 'code-review:changes-requested';
UPDATE projects SET phase = 'review:approved'          WHERE phase = 'code-review:merged';
UPDATE projects SET phase = 'security:open'            WHERE phase = 'security-review:in-progress';
UPDATE projects SET phase = 'security:approved'        WHERE phase = 'security-review:approved';
UPDATE projects SET phase = 'qa:open'                  WHERE phase = 'qa-review:in-progress';
UPDATE projects SET phase = 'qa:approved'              WHERE phase = 'qa-review:approved';

-- ── Update projects DEFAULT to new vocabulary ──────────────────────────────
-- SQLite requires table recreation to change a DEFAULT value.

CREATE TABLE projects_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    repo        TEXT,
    phase       TEXT NOT NULL DEFAULT 'design:open',
    created_by  TEXT NOT NULL REFERENCES agents(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

INSERT INTO projects_new SELECT id, name, description, repo, phase, created_by, created_at, updated_at
FROM projects;

-- NOTE: DROP TABLE projects requires no child rows referencing it via FK at migration time,
-- or PRAGMA foreign_keys=OFF. Safe on initial setup; on a live DB, ensure sessions/tasks
-- have no rows before running this migration.
DROP TABLE projects;
ALTER TABLE projects_new RENAME TO projects;
