"""Memory lane (L4, L5): embeddings, code chunks, hybrid retrieval, ingestion.

The study calls this client `TigerMemoryClient`. It is named `CodeMemory` here
because which Postgres it talks to is a connection string, not a class -- the same
code serves self-hosted timescaledb-ha and Tiger Cloud.
"""

from backend.memory.code_memory import ChunkInput, CodeMemory, IndexedFile, MemoryStats
from backend.memory.context_retriever import (
    RetrievedChunk,
    rank_only_vector,
    reciprocal_rank_fusion,
)
from backend.memory.embedder import (
    Embedder,
    FakeEmbedder,
    OpenAIEmbedder,
    get_embedder,
    tokenize,
)
from backend.memory.ingestion import (
    IngestionPipeline,
    IngestionReport,
    chunk_source,
    estimate_tokens,
    find_symbol_boundaries,
    iter_candidate_files,
)

__all__ = [
    "ChunkInput",
    "CodeMemory",
    "Embedder",
    "FakeEmbedder",
    "IndexedFile",
    "IngestionPipeline",
    "IngestionReport",
    "MemoryStats",
    "OpenAIEmbedder",
    "RetrievedChunk",
    "chunk_source",
    "estimate_tokens",
    "find_symbol_boundaries",
    "get_embedder",
    "iter_candidate_files",
    "rank_only_vector",
    "reciprocal_rank_fusion",
    "tokenize",
]
