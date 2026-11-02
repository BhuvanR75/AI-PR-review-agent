-- 003_index_diskann.sql
-- OPTIONAL lane, applied when pgvectorscale is available; the alternative is
-- 003_index_hnsw.sql. Exactly one of the two runs, chosen by the capability probe in
-- scripts/migrate.py, so neither leaves a redundant index behind.
--
-- DiskANN keeps more of the search structure on SSD instead of RAM, which is what
-- makes a large code memory affordable rather than a memory bill.

CREATE INDEX IF NOT EXISTS code_chunks_emb_idx
    ON code_chunks USING diskann (embedding vector_cosine_ops);
