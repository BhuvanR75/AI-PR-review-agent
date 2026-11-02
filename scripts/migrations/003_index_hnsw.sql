-- 003_index_hnsw.sql
-- The portable vector index, used when pgvectorscale/DiskANN is unavailable.
-- Same operator class and same query shape (`<=>` cosine distance) as the DiskANN
-- variant, so nothing above backend/memory/code_memory.py knows which one is live.

CREATE INDEX IF NOT EXISTS code_chunks_emb_idx
    ON code_chunks USING hnsw (embedding vector_cosine_ops);
