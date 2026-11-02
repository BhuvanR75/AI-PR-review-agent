"""Migration and schema tests against a real Postgres.

These are the tests that verify the one-spine thesis on the facts rather than on the
diagram: the three lanes exist in one database, the capability probe reports which
extensions are really present, and the optional lane either lands whole or not at all.

They skip when Postgres is unreachable, with the reason in the skip message.
"""

from __future__ import annotations

import uuid

import pytest

from backend.settings import get_settings
from scripts.migrate import migrate, read_capabilities, verify

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _schema(db_pool) -> None:  # type: ignore[no-untyped-def]
    """Every test in this module reads the real schema, so it is migrated once per
    test rather than by whichever test happened to ask for it first."""
    report = await migrate(get_settings().database_url)
    assert not report.failed, f"migration failed: {report.notes}"


@pytest.fixture
async def migrated(db_pool) -> dict:  # type: ignore[no-untyped-def]
    return await verify(get_settings().database_url)


async def test_core_lane_tables_exist(migrated: dict) -> None:
    tables = set(migrated["tables"])
    assert {
        "code_chunks",
        "repo_file_index",
        "pr_review_records",
        "finding_records",
        "dedup_near_misses",
        "hitl_reviews",
        "hitl_feedback",
        "webhook_deliveries",
        "agent_events",
    } <= tables


async def test_memory_lane_has_both_indexes(migrated: dict) -> None:
    """The whole point of the memory lane is that both retrieval paths are indexed."""
    indexes = set(migrated["indexes"])
    assert "code_chunks_fts_idx" in indexes
    assert "code_chunks_emb_idx" in indexes
    assert "code_chunks_unique_idx" in indexes


async def test_vector_column_is_dimensioned_as_configured(db_pool) -> None:  # type: ignore[no-untyped-def]
    dimension = await db_pool.fetchval(
        """
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'code_chunks'::regclass AND attname = 'embedding'
        """
    )
    # pgvector stores the dimension in atttypmod directly.
    assert dimension == get_settings().embedding_dim


async def test_capabilities_are_recorded_for_readiness(migrated: dict) -> None:
    capabilities = await read_capabilities(get_settings().database_url)
    assert set(capabilities) == {"timescale", "vectorscale"}
    assert all(isinstance(value, bool) for value in capabilities.values())


async def test_timescale_lane_is_either_present_or_reported_absent(
    migrated: dict, db_pool  # type: ignore[no-untyped-def]
) -> None:
    """Either the hypertable exists, or the capability says it does not -- never a
    silent third state where the lane is half-applied."""
    capabilities = await read_capabilities(get_settings().database_url)
    if capabilities.get("timescale"):
        assert "agent_events" in migrated["hypertables"]
        views = set(migrated["views"])
        assert {"agent_health_1m", "pr_cost_hourly"} <= views
    else:
        assert "agent_events" not in migrated["hypertables"]


async def test_agent_events_key_includes_the_partition_column(db_pool) -> None:  # type: ignore[no-untyped-def]
    """A hypertable cannot carry a unique index that omits the partitioning column, so
    the primary key is (id, ts) on purpose -- otherwise promoting the table later fails."""
    columns = await db_pool.fetch(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = 'agent_events'::regclass AND i.indisprimary
        """
    )
    assert {row["attname"] for row in columns} == {"id", "ts"}


async def test_event_insert_round_trips_including_jsonb(db_pool, review_id: str) -> None:  # type: ignore[no-untyped-def]
    await db_pool.execute(
        """
        INSERT INTO agent_events (review_id, agent, event_type, payload, cost_usd, confidence)
        VALUES ($1, 'security', 'llm.call', $2, 0.001234, 0.5)
        """,
        uuid.UUID(review_id),
        {"prompt_hash": "abc"},
    )
    row = await db_pool.fetchrow(
        "SELECT payload, cost_usd, id, ts FROM agent_events WHERE review_id = $1",
        uuid.UUID(review_id),
    )
    assert row["payload"] == {"prompt_hash": "abc"}
    assert float(row["cost_usd"]) == pytest.approx(0.001234)
    assert row["id"] is not None


async def test_migrations_are_idempotent(migrated: dict) -> None:
    """Re-running must be a no-op, because the runner is what operators run twice."""
    before = set(migrated["indexes"])
    report = await migrate(get_settings().database_url)
    assert report.applied == [], f"expected a no-op, applied: {report.applied}"
    after = await verify(get_settings().database_url)
    assert set(after["indexes"]) == before
