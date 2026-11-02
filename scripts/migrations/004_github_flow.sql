-- 004_github_flow.sql
-- The user-initiated flow: connect GitHub, browse repos and PRs, trigger a review, and
-- show the changed code alongside the findings. Two tables:
--
--   github_connections  the token the user pasted, one active connection. A singleton
--                       row rather than per-user rows because this deployment is a
--                       single-operator tool; the audit trail records who connected.
--   review_artifacts    the diff and changed-file list, persisted at review time so the
--                       console can always show the code a finding anchors to -- even
--                       after the PR moves on. Written once per review, never updated.
--
-- Portable core: no extensions required, runs on any Postgres.

CREATE TABLE IF NOT EXISTS github_connections (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    label        TEXT        NOT NULL DEFAULT 'default',
    user_login   TEXT        NOT NULL,
    token        TEXT        NOT NULL,
    scopes       TEXT[]      NOT NULL DEFAULT '{}',
    api_base     TEXT        NOT NULL DEFAULT 'https://api.github.com',
    connected_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS github_connections_label_idx
    ON github_connections (label);

CREATE TABLE IF NOT EXISTS review_artifacts (
    review_id     UUID        PRIMARY KEY REFERENCES pr_review_records(id) ON DELETE CASCADE,
    diff          TEXT        NOT NULL,
    changed_files JSONB       NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
