"""Persistence for the review artifact: the diff and the changed-file list.

The console shows findings anchored on changed lines, which requires the diff that
was actually reviewed -- not a re-fetch that may return a different commit. The
artifact is written at review time, read by the API, and never updated: a new push
creates a new review with its own artifact, and the old one stays as it was.

Raw asyncpg rather than the ORM, for the same reason as the connection store: two
statements, and the diff does not belong to the relational object graph the review
code navigates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import asyncpg

from backend.models.review import ChangedFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewArtifact:
    diff: str
    changed_files: list[ChangedFile] = field(default_factory=list)


class ArtifactRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(self._dsn, timeout=10)

    async def save(self, review_id: str, *, diff: str, changed_files: list[ChangedFile]) -> None:
        payload = json.dumps(
            [
                {
                    "path": f.path,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                }
                for f in changed_files
            ]
        )
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO review_artifacts (review_id, diff, changed_files)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (review_id) DO NOTHING
                """,
                review_id,
                diff,
                payload,
            )
        finally:
            await conn.close()

    async def get(self, review_id: str) -> ReviewArtifact | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                "SELECT diff, changed_files FROM review_artifacts WHERE review_id = $1",
                review_id,
            )
        finally:
            await conn.close()
        if row is None:
            return None
        files = [ChangedFile(**f) for f in json.loads(row["changed_files"] or "[]")]
        return ReviewArtifact(diff=row["diff"], changed_files=files)


__all__ = ["ArtifactRepository", "ReviewArtifact"]
