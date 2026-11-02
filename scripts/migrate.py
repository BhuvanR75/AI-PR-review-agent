"""Apply the schema, probing which lanes this Postgres can actually carry.

The point of this script is the capability probe. `001_core.sql` is portable and
always runs; the TimescaleDB lane and the DiskANN index are applied only when the
server offers them. When they are absent the system is *correct but slower* -- it
reads the same facts from `agent_events` with plain SQL and uses an HNSW index --
which is the degradation posture the study asks for.

    uv run pr-review migrate            # apply what is pending
    uv run pr-review migrate --reset    # drop the schema and rebuild
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from backend.settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

CORE_FILE = "001_core.sql"
TIMESCALE_FILE = "002_timescale_extras.sql"
DISKANN_FILE = "003_index_diskann.sql"
HNSW_FILE = "003_index_hnsw.sql"
GITHUB_FLOW_FILE = "004_github_flow.sql"

#: Extensions we try to switch on, and the capability each one unlocks.
EXT_TIMESCALE = "timescaledb"
EXT_VECTORSCALE = "vectorscale"


@dataclass
class MigrationReport:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    has_timescale: bool = False
    has_vectorscale: bool = False
    notes: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"applied:  {', '.join(self.applied) or '(none)'}"]
        if self.skipped:
            lines.append(f"skipped:  {', '.join(self.skipped)}")
        if self.failed:
            lines.append(f"failed:   {', '.join(self.failed)}")
        lines.append(f"lanes:    timescale={self.has_timescale} vectorscale={self.has_vectorscale}")
        for key, note in self.notes.items():
            lines.append(f"  note [{key}]: {note}")
        return "\n".join(lines)


async def _ensure_bookkeeping(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            name       TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            note       TEXT
        );
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_capabilities (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


async def _applied(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT name FROM _migrations")
    return {r["name"] for r in rows}


async def _record(
    conn: asyncpg.Connection, name: str, note: str | None = None, *, skipped: bool = False
) -> None:
    await conn.execute(
        """
        INSERT INTO _migrations (name, note)
        VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET note = EXCLUDED.note
        """,
        name,
        note if not skipped else f"skipped: {note}",
    )


async def _set_capability(conn: asyncpg.Connection, key: str, value: bool) -> None:
    await conn.execute(
        """
        INSERT INTO system_capabilities (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        "true" if value else "false",
    )


async def _available_extensions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT name FROM pg_available_extensions")
    return {r["name"] for r in rows}


async def _apply_file(conn: asyncpg.Connection, path: Path) -> None:
    """Run a whole .sql file atomically, via the simple query protocol
    (asyncpg allows multiple statements when no arguments are passed)."""
    sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
    async with conn.transaction():
        await conn.execute(sql)


async def _try_extension(conn: asyncpg.Connection, name: str) -> tuple[bool, str | None]:
    """Create an extension outside any transaction block. Returns (ok, error)."""
    try:
        await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {name} CASCADE")
        return True, None
    except Exception as exc:  # noqa: BLE001 - we report, never crash the migration
        return False, str(exc).strip().splitlines()[0][:300]


async def reset_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    await conn.execute("CREATE SCHEMA public")


async def migrate(
    dsn: str,
    *,
    reset: bool = False,
    probe_only: bool = False,
) -> MigrationReport:
    report = MigrationReport()
    conn = await asyncpg.connect(dsn)
    try:
        if reset:
            await reset_schema(conn)

        await _ensure_bookkeeping(conn)
        done = await _applied(conn)
        available = await _available_extensions(conn)

        if probe_only:
            report.has_timescale = EXT_TIMESCALE in available
            report.has_vectorscale = EXT_VECTORSCALE in available
            return report

        # --- lane 0: portable core (always) ---------------------------------
        if CORE_FILE not in done:
            await _apply_file(conn, MIGRATIONS_DIR / CORE_FILE)
            await _record(conn, CORE_FILE)
            report.applied.append(CORE_FILE)
        # --- lane 0b: the user-initiated flow (portable, no extensions) -------
        if GITHUB_FLOW_FILE not in done:
            await _apply_file(conn, MIGRATIONS_DIR / GITHUB_FLOW_FILE)
            await _record(conn, GITHUB_FLOW_FILE)
            report.applied.append(GITHUB_FLOW_FILE)

        # The core file must be present before the optional lanes can attach to it.
        done = await _applied(conn)

        # --- lane 1: timeseries (hypertable + rollups) ----------------------
        if TIMESCALE_FILE in done:
            report.has_timescale = True
        elif EXT_TIMESCALE in available:
            ok, error = await _try_extension(conn, EXT_TIMESCALE)
            if ok:
                try:
                    await _apply_file(conn, MIGRATIONS_DIR / TIMESCALE_FILE)
                    await _record(conn, TIMESCALE_FILE)
                    report.applied.append(TIMESCALE_FILE)
                    report.has_timescale = True
                except Exception as exc:  # noqa: BLE001
                    reason = str(exc).strip().splitlines()[0][:300]
                    await _record(conn, TIMESCALE_FILE, note=f"failed: {reason}")
                    report.failed.append(TIMESCALE_FILE)
                    report.notes[TIMESCALE_FILE] = reason
            else:
                note = f"extension unavailable: {error}"
                await _record(conn, TIMESCALE_FILE, note=note, skipped=True)
                report.skipped.append(TIMESCALE_FILE)
                report.notes[TIMESCALE_FILE] = error or "unknown"
        else:
            await _record(conn, TIMESCALE_FILE, note="not available on this server", skipped=True)
            report.skipped.append(TIMESCALE_FILE)
            report.notes[TIMESCALE_FILE] = "timescaledb not available"

        # --- lane 2: the vector index ---------------------------------------
        if DISKANN_FILE in done or HNSW_FILE in done:
            report.has_vectorscale = DISKANN_FILE in await _applied(conn)
        elif EXT_VECTORSCALE in available:
            ok, error = await _try_extension(conn, EXT_VECTORSCALE)
            if ok:
                try:
                    await _apply_file(conn, MIGRATIONS_DIR / DISKANN_FILE)
                    await _record(conn, DISKANN_FILE)
                    report.applied.append(DISKANN_FILE)
                    report.has_vectorscale = True
                except Exception as exc:  # noqa: BLE001
                    reason = str(exc).strip().splitlines()[0][:300]
                    report.notes[DISKANN_FILE] = reason
                    await _apply_file(conn, MIGRATIONS_DIR / HNSW_FILE)
                    await _record(conn, HNSW_FILE, note="diskann failed, fell back")
                    report.applied.append(HNSW_FILE)
            else:
                await _apply_file(conn, MIGRATIONS_DIR / HNSW_FILE)
                await _record(conn, HNSW_FILE, note=f"vectorscale unavailable: {error}")
                report.applied.append(HNSW_FILE)
                report.notes[HNSW_FILE] = error or "unknown"
        else:
            await _apply_file(conn, MIGRATIONS_DIR / HNSW_FILE)
            await _record(conn, HNSW_FILE, note="vectorscale not available on this server")
            report.applied.append(HNSW_FILE)
            report.notes[HNSW_FILE] = "vectorscale not available"

        await _set_capability(conn, "timescale", report.has_timescale)
        await _set_capability(conn, "vectorscale", report.has_vectorscale)
        return report
    finally:
        await conn.close()


async def read_capabilities(dsn: str) -> dict[str, bool]:
    """Cheap read for /readyz -- no extension probing at request time."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT key, value FROM system_capabilities")
        return {r["key"]: r["value"] == "true" for r in rows}
    except Exception:  # noqa: BLE001 - schema may not exist yet
        return {}
    finally:
        await conn.close()


async def verify(dsn: str) -> dict[str, object]:
    """What the schema actually looks like right now. Used by the integration tests
    and by `pr-review migrate --verify`."""
    conn = await asyncpg.connect(dsn)
    try:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY indexname"
        )
        views = await conn.fetch(
            "SELECT viewname FROM pg_views WHERE schemaname = 'public' ORDER BY viewname"
        )
        hypertables: list[str] = []
        try:
            rows = await conn.fetch(
                "SELECT hypertable_name FROM timescaledb_information.hypertables"
            )
            hypertables = [r["hypertable_name"] for r in rows]
        except Exception:  # noqa: BLE001 - extension absent
            pass
        return {
            "tables": [r["tablename"] for r in tables],
            "indexes": [r["indexname"] for r in indexes],
            "views": [r["viewname"] for r in views],
            "hypertables": hypertables,
            "migrations": sorted(await _applied(conn))
            if "pr_review_records" in {r["tablename"] for r in tables}
            else [],
        }
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply schema migrations with a capability probe.")
    parser.add_argument("--reset", action="store_true", help="drop and rebuild the public schema")
    parser.add_argument("--probe", action="store_true", help="only report available extensions")
    parser.add_argument("--verify", action="store_true", help="print the resulting schema")
    args = parser.parse_args(argv)

    settings = get_settings()
    report = asyncio.run(migrate(settings.database_url, reset=args.reset, probe_only=args.probe))
    print(report.summary())

    if args.verify:
        import json

        print(json.dumps(asyncio.run(verify(settings.database_url)), indent=2))

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
