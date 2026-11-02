"""The GitHub connection store.

One active connection, in `github_connections` on the same Postgres spine as
everything else. The token is stored as the user pasted it -- for a local, single
operator tool whose whole database already contains the repositories it reviews,
that is the honest threat model; obfuscation here would be theatre. It is never
returned in full by any API response (only a fingerprint), and it is never logged.

The store is deliberately asyncpg-raw rather than ORM: it is two statements, and
keeping it out of `Base.metadata` keeps the ORM mapping 1:1 with the review tables.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)

#: The columns a fingerprint may carry. The token is deliberately not among them.
_PUBLIC_COLUMNS = "id, label, user_login, scopes, api_base, connected_by, created_at, updated_at"


def token_fingerprint(token: str) -> str:
    """A short, non-reversible tag so the console can show *which* token is connected
    without ever returning it."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Connection:
    label: str
    user_login: str
    token: str
    scopes: list[str]
    api_base: str


@dataclass(frozen=True)
class ConnectionPublic:
    """What the API may return: the token reduced to a fingerprint."""

    label: str
    user_login: str
    scopes: list[str]
    fingerprint: str
    created_at: object | None = None


class ConnectionStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(self._dsn, timeout=10)

    async def get_active(self) -> Connection | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT label, user_login, token, scopes, api_base
                FROM github_connections
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        finally:
            await conn.close()
        if row is None:
            return None
        return Connection(
            label=row["label"],
            user_login=row["user_login"],
            token=row["token"],
            scopes=list(row["scopes"] or []),
            api_base=row["api_base"],
        )

    async def save(
        self,
        *,
        token: str,
        user_login: str,
        scopes: list[str],
        api_base: str,
        label: str = "default",
        connected_by: str = "console",
    ) -> None:
        """Upsert the single active connection. Replacing is the normal path: a user
        rotates a token by pasting a new one."""
        conn = await self._connect()
        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM github_connections WHERE label = $1", label)
                await conn.execute(
                    """
                    INSERT INTO github_connections
                        (label, user_login, token, scopes, api_base, connected_by)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    label,
                    user_login,
                    token,
                    scopes,
                    api_base,
                    connected_by,
                )
        finally:
            await conn.close()
        logger.info(
            "github connection saved",
            extra={"context": {"user": user_login, "fingerprint": token_fingerprint(token)}},
        )

    async def delete(self, label: str = "default") -> bool:
        conn = await self._connect()
        try:
            status = await conn.execute("DELETE FROM github_connections WHERE label = $1", label)
        finally:
            await conn.close()
        return status.endswith("1")

    async def describe(self) -> ConnectionPublic | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"""
                SELECT {_PUBLIC_COLUMNS}, token
                FROM github_connections
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        finally:
            await conn.close()
        if row is None:
            return None
        return ConnectionPublic(
            label=row["label"],
            user_login=row["user_login"],
            scopes=list(row["scopes"] or []),
            fingerprint=token_fingerprint(row["token"]),
            created_at=row["created_at"],
        )


__all__ = ["Connection", "ConnectionPublic", "ConnectionStore", "token_fingerprint"]
