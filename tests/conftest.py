"""Shared fixtures.

Every test runs with fake providers and dry-run GitHub, so the unit suite needs no
secrets, no network and no database. Integration tests ask for the real Postgres and
skip with an explicit reason when it is not reachable, rather than failing -- the
distinction matters on a machine where Docker is not running.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.settings import Settings, get_settings, reload_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("GITHUB_CLIENT_MODE", "dry_run")
    monkeypatch.setenv("HITL_MODE", "always")
    monkeypatch.setenv("HITL_CONFIDENCE_THRESHOLD", "0.75")
    monkeypatch.setenv("DAILY_BUDGET_USD", "25")
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/prreview_test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6380/1")
    reload_settings()
    yield get_settings()
    reload_settings()


class FakePool:
    """Records what the events spine would have written.

    Enough surface for `emit_agent_event`, the cost queries and the trace reads, so the
    observability layer can be tested without a database.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []
        self.executed: list[str] = []
        self.enqueued: list[tuple[str, list[Any]]] = []
        self.fetch_rows: list[dict[str, Any]] = []
        self.fetchval_result: Any = None

    # Full statements are kept (not truncated) so tests can assert on the parts of the
    # SQL that matter, such as the ON CONFLICT clause that makes idempotency correct.
    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append(" ".join(query.split()).upper())
        self.rows.append(args)
        return "INSERT 0 1"

    async def executemany(self, query: str, args: list[Any]) -> None:
        self.enqueued.append((" ".join(query.split()), list(args)))
        self.rows.extend(tuple(row) for row in args)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append(" ".join(query.split()).upper())
        return list(self.fetch_rows)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append(" ".join(query.split()).upper())
        return self.fetch_rows[0] if self.fetch_rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed.append(" ".join(query.split()).upper())
        return self.fetchval_result

    def record_count(self) -> int:
        return len(self.rows)


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def patched_event_pool(monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool) -> FakePool:
    """Route the events spine at a FakePool instead of a real connection."""
    import backend.observability.events as events_module

    monkeypatch.setattr(events_module, "get_pool", lambda: fake_pool)
    return fake_pool


@pytest.fixture
def review_id() -> str:
    return str(uuid.uuid4())


#: Probed once per session. Without this, a machine without Docker spends the
#: connection timeout on every integration test and the skip itself becomes the reason
#: nobody runs the suite.
_db_probe: tuple[bool, str] | None = None


async def database_available() -> tuple[bool, str]:
    """Whether the compose Postgres is reachable, with a reason if it is not."""
    global _db_probe
    if _db_probe is not None:
        return _db_probe

    import asyncpg

    settings = get_settings()
    try:
        conn = await asyncpg.connect(settings.database_url, timeout=3)
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        _db_probe = (False, f"{type(exc).__name__}: {exc}")
    else:
        await conn.close()
        _db_probe = (True, "ok")
    return _db_probe


@pytest.fixture
async def db_pool():  # type: ignore[no-untyped-def]
    """A real asyncpg pool, or a skip with an actionable message."""
    import asyncpg

    from backend.database.pg import close_pool

    ok, reason = await database_available()
    if not ok:
        pytest.skip(
            "Postgres is not reachable, so this integration test is skipped. "
            f"Start it with `docker compose up -d` (reason: {reason})"
        )
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()
        await close_pool()
