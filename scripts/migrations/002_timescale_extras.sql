-- 002_timescale_extras.sql
-- OPTIONAL lane. Applied only when the capability probe finds TimescaleDB.
-- Everything here is a performance lane, not a correctness lane: without it the
-- system reads the same facts from agent_events with plain SQL, just slower.
--
-- The CREATE EXTENSION statement is deliberately NOT here: it is issued by
-- scripts/migrate.py outside any transaction block (Postgres is happier that way),
-- and this file is then applied as one atomic transaction -- the lane either lands
-- whole or not at all, so the schema is never left half-migrated.

-- Partition the spine by time. Recent-window queries touch recent chunks instead
-- of dragging the whole history through the scan.
SELECT create_hypertable(
    'agent_events',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Rollups. A dashboard must not sum ten million rows on every page load.
--
-- Note on percentiles: the study's sketch uses approx_percentile(percentile_agg(...)),
-- which lives in timescaledb_toolkit -- a separate extension that is not guaranteed
-- to be present. Continuous aggregates also cannot use ordered-set aggregates or
-- count(DISTINCT ...). So the rollup keeps avg/max here, and p95 is computed exactly
-- from the raw hypertable over a bounded window in economics/cost_repository.py where
-- a time-bounded scan is cheap. Swap in percentile_agg behind the same repository
-- method once the toolkit is confirmed present.
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)                              AS bucket,
    agent,
    count(*) FILTER (WHERE event_type = 'llm.call')          AS llm_calls,
    sum(cost_usd)                                            AS cost_usd,
    sum(tokens_in)                                           AS tokens_in,
    sum(tokens_out)                                          AS tokens_out,
    avg(latency_ms)                                          AS avg_latency_ms,
    max(latency_ms)                                          AS max_latency_ms,
    count(*) FILTER (WHERE outcome = 'rejected')             AS rejected,
    count(*) FILTER (WHERE outcome = 'escalated')            AS escalated,
    count(*) FILTER (WHERE outcome IS NOT NULL)              AS decisions
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'agent_health_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

-- Per-PR cost, refreshed hourly: the BudgetGuard and the economics page read it.
CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)                        AS bucket,
    review_id,
    sum(cost_usd)                                    AS total_cost_usd,
    count(*) FILTER (WHERE event_type = 'llm.call')  AS llm_calls,
    max(confidence)                                  AS max_confidence
FROM agent_events
WHERE review_id IS NOT NULL
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'pr_cost_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists     => TRUE
);

-- Older events are read rarely and compress well.
ALTER TABLE agent_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'agent,review_id'
);

SELECT add_compression_policy('agent_events', INTERVAL '30 days', if_not_exists => TRUE);
