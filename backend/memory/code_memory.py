"""Code memory: the vector lane of the one spine.

This is the module the study calls `TigerMemoryClient`; the name changed because
what it talks to is a repository of code chunks, and which Postgres is underneath --
self-hosted timescaledb-ha or Tiger Cloud -- is a connection string, not a
different class.

Everything here is raw asyncpg by design. Embeddings travel as `float4[]` and are
cast explicitly (`CAST($n::float4[] AS vector)`), so nothing depends on a driver
type codec for pgvector, and the embedding column is never returned -- only the
scores are, which keeps reads cheap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from backend.memory.context_retriever import RetrievedChunk, reciprocal_rank_fusion
from backend.memory.embedder import Embedder
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkInput:
    path: str
    chunk_index: int
    content: str
    symbol: str | None = None
    token_count: int | None = None


@dataclass(frozen=True)
class IndexedFile:
    path: str
    content_hash: str
    size_bytes: int


@dataclass
class MemoryStats:
    repo: str
    chunks: int
    files: int
    symbols: int
    last_updated: str | None


_UPSERT_SQL = """
INSERT INTO code_chunks (repo, path, symbol, chunk_index, content, embedding, token_count)
VALUES ($1, $2, $3, $4, $5, CAST($6::float4[] AS vector), $7)
ON CONFLICT (repo, path, chunk_index) DO UPDATE SET
    symbol      = EXCLUDED.symbol,
    content     = EXCLUDED.content,
    embedding   = EXCLUDED.embedding,
    token_count = EXCLUDED.token_count,
    updated_at  = now()
"""

# The vector lane. `<=>` is cosine distance; 1 - distance is similarity, only for
# reporting -- ordering is by distance so the index is usable.
_VECTOR_SQL = """
SELECT id::text AS id, path, symbol, content,
       1 - (embedding <=> CAST($2::float4[] AS vector)) AS score
FROM code_chunks
WHERE repo = $1
ORDER BY embedding <=> CAST($2::float4[] AS vector)
LIMIT $3
"""

# The keyword lane, served by the GIN index on content_tsv.
_KEYWORD_SQL = """
SELECT id::text AS id, path, symbol, content,
       ts_rank_cd(content_tsv, plainto_tsquery('english', $2)) AS score
FROM code_chunks
WHERE repo = $1 AND content_tsv @@ plainto_tsquery('english', $2)
ORDER BY score DESC
LIMIT $3
"""

_FILES_SQL = "SELECT path, content_hash, size_bytes FROM repo_file_index WHERE repo = $1"

_TOUCH_FILES_SQL = """
INSERT INTO repo_file_index (repo, path, content_hash, size_bytes, last_indexed_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (repo, path) DO UPDATE SET
    content_hash    = EXCLUDED.content_hash,
    size_bytes      = EXCLUDED.size_bytes,
    last_indexed_at = now()
"""


class PoolLike(Protocol):
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...
    async def execute(self, query: str, *args: Any) -> Any: ...
    async def executemany(self, query: str, args: list[Any]) -> Any: ...
    def acquire(self) -> Any: ...


class CodeMemory:
    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: Embedder,
        settings: Settings | None = None,
    ) -> None:
        self.pool = pool
        self.embedder = embedder
        self.settings = settings or get_settings()

    # --- writes --------------------------------------------------------------
    async def upsert_chunks(self, repo: str, chunks: list[ChunkInput]) -> int:
        """Embed and upsert in batches. Idempotent on (repo, path, chunk_index), so a
        re-index overwrites stale chunks rather than duplicating them."""
        if not chunks:
            return 0

        total = 0
        batch_size = 32
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = await self.embedder.embed([c.content for c in batch])
            rows = [
                (
                    repo,
                    chunk.path,
                    chunk.symbol,
                    chunk.chunk_index,
                    chunk.content,
                    [float(x) for x in vector],
                    chunk.token_count,
                )
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            await self.pool.executemany(_UPSERT_SQL, rows)
            total += len(rows)
        return total

    async def delete_file_chunks(self, repo: str, path: str, *, keep: int = 0) -> None:
        """Drop chunks beyond `keep` -- used when a file shrinks on re-index."""
        await self.pool.execute(
            "DELETE FROM code_chunks WHERE repo = $1 AND path = $2 AND chunk_index >= $3",
            repo,
            path,
            keep,
        )

    async def clear_repo(self, repo: str) -> None:
        await self.pool.execute("DELETE FROM code_chunks WHERE repo = $1", repo)
        await self.pool.execute("DELETE FROM repo_file_index WHERE repo = $1", repo)

    async def record_files(self, repo: str, files: list[IndexedFile]) -> None:
        if not files:
            return
        await self.pool.executemany(
            _TOUCH_FILES_SQL,
            [(repo, f.path, f.content_hash, f.size_bytes) for f in files],
        )

    # --- reads ---------------------------------------------------------------
    async def search_vector(self, repo: str, query: str, limit: int) -> list[RetrievedChunk]:
        vectors = await self.embedder.embed([query])
        rows = await self.pool.fetch(_VECTOR_SQL, repo, [float(x) for x in vectors[0]], limit)
        return [
            RetrievedChunk(
                id=r["id"],
                path=r["path"],
                symbol=r["symbol"],
                content=r["content"],
                score=float(r["score"] or 0.0),
            )
            for r in rows
        ]

    async def search_keyword(self, repo: str, query: str, limit: int) -> list[RetrievedChunk]:
        rows = await self.pool.fetch(_KEYWORD_SQL, repo, query, limit)
        return [
            RetrievedChunk(
                id=r["id"],
                path=r["path"],
                symbol=r["symbol"],
                content=r["content"],
                score=float(r["score"] or 0.0),
            )
            for r in rows
        ]

    async def search(
        self,
        repo: str,
        query: str,
        *,
        k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Hybrid search. Runs both lanes, fuses by reciprocal rank, returns top-k.

        The keyword lane is allowed to fail without failing the search: a missing
        tsvector index degrades recall, not availability. That is the general
        posture -- slower or narrower, never unavailable.
        """
        limit = k or self.settings.top_k_chunks
        fetch = max(limit * 3, limit)
        vector_hits = await self.search_vector(repo, query, fetch)
        keyword_hits: list[RetrievedChunk] = []
        try:
            keyword_hits = await self.search_keyword(repo, query, fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("keyword lane unavailable, continuing with vectors only: %s", exc)
        return reciprocal_rank_fusion(
            vector_hits,
            keyword_hits,
            k=self.settings.rrf_k,
            limit=limit,
        )

    async def indexed_files(self, repo: str) -> dict[str, str]:
        rows = await self.pool.fetch(_FILES_SQL, repo)
        return {r["path"]: r["content_hash"] for r in rows}

    async def stats(self, repo: str) -> MemoryStats:
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS chunks,
                   count(DISTINCT path) AS files,
                   count(DISTINCT symbol) AS symbols,
                   max(updated_at) AS last_updated
            FROM code_chunks WHERE repo = $1
            """,
            repo,
        )
        return MemoryStats(
            repo=repo,
            chunks=int(row["chunks"] or 0),
            files=int(row["files"] or 0),
            symbols=int(row["symbols"] or 0),
            last_updated=str(row["last_updated"]) if row["last_updated"] else None,
        )

    async def has_repo(self, repo: str) -> bool:
        value = await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM code_chunks WHERE repo = $1 LIMIT 1)", repo
        )
        return bool(value)
