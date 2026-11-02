"""Hybrid retrieval: vector and keyword lanes, fused by reciprocal rank.

Pure vector search finds meaning but is bad at exact identifiers -- function names,
error codes, config keys. Pure keyword search finds those and misses paraphrase. A
codebase diff contains both, so the two lanes run and the results are fused.

Fusion is by reciprocal rank rather than by score, because the two lanes' scores are
not comparable: cosine similarity and ts_rank_cd live on different scales, and
normalising them onto one is a fudge that shifts with every corpus. Rank is a common
currency, and RRF needs no tuning beyond k.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    path: str
    symbol: str | None
    content: str
    score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None
    vector_score: float | None = None
    keyword_score: float | None = None

    @property
    def label(self) -> str:
        return f"{self.path}:{self.symbol}" if self.symbol else self.path

    def as_prompt_line(self) -> str:
        header = f"--- {self.path}"
        if self.symbol:
            header += f" :: {self.symbol}"
        return f"{header}\n{self.content.strip()}"


def reciprocal_rank_fusion(
    vector_hits: list[RetrievedChunk],
    keyword_hits: list[RetrievedChunk],
    *,
    k: int = 60,
    limit: int | None = None,
    weight_vector: float = 1.0,
    weight_keyword: float = 1.0,
) -> list[RetrievedChunk]:
    """Fuse two ranked lists. `k` damps the contribution of deep ranks (the standard
    RRF constant); weights let the vector lane lead without silencing the keyword lane."""
    fused: dict[str, float] = {}
    seen: dict[str, RetrievedChunk] = {}

    def contribute(hits: list[RetrievedChunk], weight: float, lane: str) -> None:
        for rank, hit in enumerate(hits, start=1):
            fused[hit.id] = fused.get(hit.id, 0.0) + weight / (k + rank)
            existing = seen.get(hit.id)
            if existing is None:
                seen[hit.id] = RetrievedChunk(
                    id=hit.id,
                    path=hit.path,
                    symbol=hit.symbol,
                    content=hit.content,
                    vector_rank=rank if lane == "vector" else None,
                    keyword_rank=rank if lane == "keyword" else None,
                    vector_score=hit.score if lane == "vector" else None,
                    keyword_score=hit.score if lane == "keyword" else None,
                )
            else:
                seen[hit.id] = RetrievedChunk(
                    id=existing.id,
                    path=existing.path,
                    symbol=existing.symbol,
                    content=existing.content,
                    vector_rank=rank if lane == "vector" else existing.vector_rank,
                    keyword_rank=rank if lane == "keyword" else existing.keyword_rank,
                    vector_score=hit.score if lane == "vector" else existing.vector_score,
                    keyword_score=hit.score if lane == "keyword" else existing.keyword_score,
                )

    contribute(vector_hits, weight_vector, "vector")
    contribute(keyword_hits, weight_keyword, "keyword")

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    results = [
        RetrievedChunk(
            id=chunk.id,
            path=chunk.path,
            symbol=chunk.symbol,
            content=chunk.content,
            score=score,
            vector_rank=chunk.vector_rank,
            keyword_rank=chunk.keyword_rank,
            vector_score=chunk.vector_score,
            keyword_score=chunk.keyword_score,
        )
        for chunk_id, score in ordered
        for chunk in (seen[chunk_id],)
    ]
    return results[:limit] if limit is not None else results


def rank_only_vector(hits: list[RetrievedChunk], limit: int | None = None) -> list[RetrievedChunk]:
    """Degradation path: when the keyword lane is unavailable (index still building),
    vector results are still returned rather than nothing."""
    return hits[:limit] if limit is not None else hits
