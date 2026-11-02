-- 001_core.sql
-- The portable core of the one data spine (ADR-003). Runs on any Postgres with pgvector.
-- The Tiger-specific lanes live in 002_timescale_extras.sql and 003_index_diskann.sql and
-- are applied only when the capability probe finds them; the system is correct without them.
--
-- Three lanes, one database:
--   memory (semantic)  -> code_chunks + repo_file_index
--   truth  (episodic)  -> pr_review_records, finding_records, hitl_*, webhook_deliveries
--   time   (the spine) -> agent_events
--
-- Every statement is idempotent so this file is safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Lane 1: memory. Code chunks, embedded for semantic retrieval.
-- ---------------------------------------------------------------------------
-- NOTE: embeddings are written and read only through raw asyncpg in
-- backend/memory/code_memory.py, which casts a float4[] parameter to vector.
-- Keeping the vector column out of the ORM avoids needing a codec for a type
-- whose OID is not known to the driver.
CREATE TABLE IF NOT EXISTS code_chunks (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    repo        TEXT        NOT NULL,
    path        TEXT        NOT NULL,
    symbol      TEXT,
    chunk_index INT         NOT NULL,
    content     TEXT        NOT NULL,
    embedding   vector(256) NOT NULL,
    token_count INT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE UNIQUE INDEX IF NOT EXISTS code_chunks_unique_idx
    ON code_chunks (repo, path, chunk_index);

CREATE INDEX IF NOT EXISTS code_chunks_repo_idx ON code_chunks (repo);

-- The keyword lane. Vector search finds meaning; this finds exact identifiers,
-- error codes and config keys, which embeddings are bad at.
CREATE INDEX IF NOT EXISTS code_chunks_fts_idx ON code_chunks USING GIN (content_tsv);

-- Freshness tracking: only re-embed files whose content hash changed.
CREATE TABLE IF NOT EXISTS repo_file_index (
    repo            TEXT        NOT NULL,
    path            TEXT        NOT NULL,
    content_hash    TEXT        NOT NULL,
    size_bytes      INT,
    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, path)
);

-- ---------------------------------------------------------------------------
-- Lane 2: truth. Ordinary relational data, ordinary transactions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT        PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    action      TEXT,
    repo        TEXT,
    pr_number   INT,
    status      TEXT        NOT NULL DEFAULT 'enqueued',
    reason      TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pr_review_records (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    repo               TEXT        NOT NULL,
    pr_number          INT         NOT NULL,
    head_sha           TEXT        NOT NULL,
    base_sha           TEXT,
    title              TEXT,
    author             TEXT,
    delivery_id        TEXT,
    status             TEXT        NOT NULL DEFAULT 'queued',
    decision           TEXT,
    hitl_mode          TEXT,
    overall_confidence NUMERIC(4,3),
    github_review_id   TEXT,
    github_review_url  TEXT,
    error              TEXT,
    posted_at          TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One review per (repo, pr, head_sha): a redelivered webhook and a re-run of the
-- same commit land on the same row instead of producing two reviews.
CREATE UNIQUE INDEX IF NOT EXISTS pr_review_head_idx
    ON pr_review_records (repo, pr_number, head_sha);

CREATE INDEX IF NOT EXISTS pr_review_status_idx
    ON pr_review_records (status, created_at DESC);

CREATE TABLE IF NOT EXISTS finding_records (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id   UUID        NOT NULL REFERENCES pr_review_records(id) ON DELETE CASCADE,
    agent_type  TEXT        NOT NULL,
    severity    TEXT        NOT NULL,
    category    TEXT        NOT NULL,
    summary     TEXT        NOT NULL,
    file_path   TEXT        NOT NULL,
    line_start  INT,
    line_end    INT,
    suggestion  TEXT,
    confidence  NUMERIC(4,3) NOT NULL,
    rationale   TEXT        NOT NULL,
    dedup_key   TEXT        NOT NULL,
    agreed_by   TEXT[]      NOT NULL DEFAULT '{}',
    incomplete  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finding_records_review_idx ON finding_records (review_id);

-- Misses are logged, not silently merged: the aggregator keys dedup on
-- (file, anchor line, category) and near-misses are recorded here so the rule can
-- be revisited with real data instead of guesses.
CREATE TABLE IF NOT EXISTS dedup_near_misses (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id          UUID        NOT NULL REFERENCES pr_review_records(id) ON DELETE CASCADE,
    kept_dedup_key     TEXT        NOT NULL,
    dropped_dedup_key  TEXT        NOT NULL,
    dropped_agent      TEXT        NOT NULL,
    similarity_reason  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hitl_reviews (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id  UUID        NOT NULL UNIQUE REFERENCES pr_review_records(id) ON DELETE CASCADE,
    state      TEXT        NOT NULL DEFAULT 'awaiting',
    reason     TEXT,
    assigned_to TEXT,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_reviews_state_idx ON hitl_reviews (state, created_at);

CREATE TABLE IF NOT EXISTS hitl_feedback (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id  UUID        NOT NULL REFERENCES pr_review_records(id) ON DELETE CASCADE,
    finding_id UUID        REFERENCES finding_records(id) ON DELETE SET NULL,
    actor      TEXT        NOT NULL,
    kind       TEXT        NOT NULL,
    comment    TEXT,
    payload    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_feedback_review_idx ON hitl_feedback (review_id);

-- ---------------------------------------------------------------------------
-- Lane 3: time. The events spine. Append-only by construction: there is no
-- UPDATE or DELETE path in the application code.
-- ---------------------------------------------------------------------------
-- The primary key includes ts: a hypertable cannot carry a unique index that
-- omits the partitioning column, and we want this table to be promotable to a
-- hypertable the moment TimescaleDB is available.
CREATE TABLE IF NOT EXISTS agent_events (
    id         BIGSERIAL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_id  UUID,
    agent      TEXT        NOT NULL,
    span_id    UUID        NOT NULL DEFAULT gen_random_uuid(),
    parent_span UUID,
    event_type TEXT        NOT NULL,
    model      TEXT,
    tokens_in  INT,
    tokens_out INT,
    cost_usd   NUMERIC(10,6),
    latency_ms INT,
    outcome    TEXT,
    confidence NUMERIC(4,3),
    payload    JSONB,
    PRIMARY KEY (id, ts)
);

CREATE INDEX IF NOT EXISTS agent_events_review_idx ON agent_events (review_id, ts);
CREATE INDEX IF NOT EXISTS agent_events_ts_idx ON agent_events (ts DESC);
CREATE INDEX IF NOT EXISTS agent_events_agent_idx ON agent_events (agent, ts DESC);
