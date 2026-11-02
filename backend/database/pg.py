"""The raw asyncpg pool: one connection pool for the hot paths.

Where SQLAlchemy owns the relational truth lane, this pool owns the three paths
that are either append-heavy or touch pgvector:

  * `code_chunks` upserts and the vector/keyword search (memory lane)
  * `agent_events` appends and reads (time lane)
  * `webhook_deliveries` reservation (ingress idempotency)

The pgvector question, since it is the reason this module exists at all: the
driver has no codec for the `vector` type, so a parameter that Postgres infers as
`vector` fails. Queries here therefore pass embeddings as `float4[]` and cast
explicitly -- `CAST($1::float4[] AS vector)` -- and never SELECT the embedding
column. No type codec is registered anywhere, so nothing depends on the extension
being installed at pool-creation time.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from backend.settings import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Return JSONB as dicts rather than strings, so event payloads round-trip.
    try:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
    except Exception as exc:  # noqa: BLE001 - payloads still work as raw strings
        logger.warning("could not register jsonb codec: %s", exc)


async def create_pool(
    dsn: str | None = None,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        dsn or settings.database_url,
        min_size=min_size if min_size is not None else settings.db_pool_min_size,
        max_size=max_size if max_size is not None else settings.db_pool_max_size,
        init=_init_connection,
        command_timeout=30,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("asyncpg pool not initialised; call create_pool() at startup")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = None


async def check_pool() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


async def read_capabilities() -> dict[str, bool]:
    """Which optional lanes are live, as recorded by the migration runner."""
    pool = get_pool()
    try:
        rows = await pool.fetch("SELECT key, value FROM system_capabilities")
    except Exception:  # noqa: BLE001 - pre-migration
        return {}
    return {r["key"]: r["value"] == "true" for r in rows}


def as_float(value: Any) -> float | None:
    """NUMERIC comes back as Decimal; the API contract is float."""
    if value is None:
        return None
    return float(value)
