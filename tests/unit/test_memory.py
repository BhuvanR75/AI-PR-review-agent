"""Memory-lane tests.

The embedder and the fusion logic are the two places where "grounding" either works or
silently degrades into plausible noise, so both are tested for ordering behaviour
rather than for shapes.
"""

from __future__ import annotations

import math
from pathlib import Path

from backend.memory.code_memory import ChunkInput
from backend.memory.context_retriever import RetrievedChunk, reciprocal_rank_fusion
from backend.memory.embedder import FakeEmbedder, tokenize
from backend.memory.ingestion import (
    IngestionPipeline,
    chunk_source,
    find_symbol_boundaries,
    iter_candidate_files,
)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- embedder ----------------------------------------------------------------
async def test_fake_embeddings_are_deterministic() -> None:
    embedder = FakeEmbedder(256)
    (first,) = await embedder.embed(["charge_customer(amount)"])
    (second,) = await FakeEmbedder(256).embed(["charge_customer(amount)"])
    assert first == second


async def test_embedding_has_the_configured_dimension_and_is_normalized() -> None:
    embedder = FakeEmbedder(256)
    (vector,) = await embedder.embed(["def charge(): pass"])
    assert len(vector) == 256
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)


async def test_similar_text_embeds_closer_than_unrelated_text() -> None:
    """This is the property that makes retrieval tests meaningful offline."""
    embedder = FakeEmbedder(256)
    base, related, unrelated = await embedder.embed(
        [
            "def charge_customer(customer_id, amount):",
            "def charge_customer_refund(customer_id, amount):",
            "def render_invoice_pdf(document):",
        ]
    )
    assert cosine(base, related) > cosine(base, unrelated)


async def test_identical_text_is_maximally_similar() -> None:
    embedder = FakeEmbedder(256)
    first, second = await embedder.embed(["select * from accounts", "select * from accounts"])
    assert math.isclose(cosine(first, second), 1.0, rel_tol=1e-6)


async def test_batches_preserve_order() -> None:
    embedder = FakeEmbedder(64)
    vectors = await embedder.embed(["alpha", "beta", "gamma"])
    assert len(vectors) == 3
    assert vectors[0] != vectors[1]


def test_tokenizer_splits_identifiers() -> None:
    tokens = tokenize("chargeCustomer_refreshSession")
    assert "charge" in tokens
    assert "customer" in tokens
    assert "refresh" in tokens
    assert "session" in tokens


def test_tokenizer_drops_single_characters() -> None:
    assert "a" not in tokenize("a b c")


# --- fusion ------------------------------------------------------------------
def chunk(chunk_id: str, path: str = "app.py", score: float = 1.0) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk_id, path=path, symbol=None, content=f"body {chunk_id}", score=score
    )


def test_fusion_prefers_results_found_by_both_lanes() -> None:
    vector = [chunk("v-only"), chunk("both")]
    keyword = [chunk("k-only"), chunk("both")]
    fused = reciprocal_rank_fusion(vector, keyword, k=60)
    assert fused[0].id == "both"


def test_fusion_records_which_lane_found_what() -> None:
    fused = reciprocal_rank_fusion([chunk("a")], [chunk("b")], k=60)
    by_id = {c.id: c for c in fused}
    assert by_id["a"].vector_rank == 1
    assert by_id["a"].keyword_rank is None
    assert by_id["b"].keyword_rank == 1
    assert by_id["b"].vector_rank is None


def test_fusion_respects_the_limit() -> None:
    vector = [chunk(f"v{i}") for i in range(10)]
    keyword = [chunk(f"k{i}") for i in range(10)]
    assert len(reciprocal_rank_fusion(vector, keyword, limit=5)) == 5


def test_fusion_with_one_empty_lane_returns_the_other() -> None:
    fused = reciprocal_rank_fusion([chunk("a"), chunk("b")], [], k=60)
    assert [c.id for c in fused] == ["a", "b"]


def test_fusion_with_no_results() -> None:
    assert reciprocal_rank_fusion([], []) == []


def test_larger_k_flattens_the_rank_difference() -> None:
    """With a large k, being first matters less -- that is the RRF damper working."""
    vector = [chunk("first"), chunk("second")]
    sharp = reciprocal_rank_fusion(vector, [], k=1)
    flat = reciprocal_rank_fusion(vector, [], k=1000)
    sharp_gap = sharp[0].score - sharp[1].score
    flat_gap = flat[0].score - flat[1].score
    assert sharp_gap > flat_gap


# --- chunking ----------------------------------------------------------------
PY_SOURCE = '''"""Module docstring."""

import os

CONSTANT = 1


def first_function(value):
    """Do a thing."""
    return value + 1


def second_function(value):
    return value * 2


class Widget:
    def method(self):
        return None
'''


def test_finds_top_level_symbols_only() -> None:
    boundaries = find_symbol_boundaries(PY_SOURCE.splitlines())
    assert [name for _line, name in boundaries] == ["first_function", "second_function", "Widget"]


def test_chunks_are_labelled_with_their_symbol() -> None:
    chunks = chunk_source("app.py", PY_SOURCE)
    symbols = [c.symbol for c in chunks]
    assert "first_function" in symbols
    assert "second_function" in symbols
    assert "Widget" in symbols


def test_chunking_is_deterministic() -> None:
    first = chunk_source("app.py", PY_SOURCE)
    second = chunk_source("app.py", PY_SOURCE)
    assert [(c.chunk_index, c.content, c.symbol) for c in first] == [
        (c.chunk_index, c.content, c.symbol) for c in second
    ]


def test_chunk_indexes_are_contiguous_from_zero() -> None:
    chunks = chunk_source("app.py", PY_SOURCE)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_preamble_before_the_first_symbol_is_its_own_chunk() -> None:
    chunks = chunk_source("app.py", PY_SOURCE)
    assert chunks[0].symbol is None
    assert "import os" in chunks[0].content


def test_long_functions_are_split() -> None:
    body = "\n".join(f"    line_{i} = {i}" for i in range(200))
    source = f"def huge():\n{body}\n"
    chunks = chunk_source("app.py", source, max_lines=50)
    assert len(chunks) > 1
    assert all(c.symbol == "huge" for c in chunks)


def test_empty_and_whitespace_files_produce_nothing() -> None:
    assert chunk_source("app.py", "") == []
    assert chunk_source("app.py", "\n\n   \n") == []


def test_token_estimate_is_positive() -> None:
    chunks = chunk_source("app.py", PY_SOURCE)
    assert all((c.token_count or 0) > 0 for c in chunks)


def test_file_discovery_respects_ignores(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")

    found = {p.relative_to(tmp_path).as_posix() for p in iter_candidate_files(tmp_path)}
    assert found == {"src/app.py"}


class StubMemory:
    """Just enough CodeMemory to exercise the pipeline's decisions."""

    def __init__(self, indexed: dict[str, str] | None = None) -> None:
        self._indexed = indexed or {}
        self.written: list[ChunkInput] = []
        self.touched: list[str] = []
        self.deleted: list[tuple[str, int]] = []

        class _Pool:
            async def execute(self_inner, *args: object) -> str:
                return "DELETE 0"

        self.pool = _Pool()

    async def indexed_files(self, repo: str) -> dict[str, str]:
        return self._indexed

    async def upsert_chunks(self, repo: str, chunks: list[ChunkInput]) -> int:
        self.written.extend(chunks)
        return len(chunks)

    async def record_files(self, repo: str, files: list[object]) -> None:
        self.touched.extend(f.path for f in files)  # type: ignore[attr-defined]

    async def delete_file_chunks(self, repo: str, path: str, *, keep: int = 0) -> None:
        self.deleted.append((path, keep))


async def test_pipeline_skips_unchanged_files(tmp_path: Path) -> None:
    """Incrementality is what keeps a re-index affordable, and what stops stale chunks
    from silently grounding findings in a refactored version of the file."""
    import hashlib

    source = tmp_path / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    memory = StubMemory(indexed={"app.py": digest})
    report = await IngestionPipeline(memory).index_repo(tmp_path, "acme/shop")  # type: ignore[arg-type]

    assert report.files_skipped == 1
    assert report.files_indexed == 0
    assert memory.written == []


async def test_pipeline_indexes_changed_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    memory = StubMemory()

    report = await IngestionPipeline(memory).index_repo(tmp_path, "acme/shop")  # type: ignore[arg-type]

    assert report.files_indexed == 1
    assert report.chunks_written >= 1
    assert memory.touched == ["app.py"]


async def test_pipeline_removes_files_that_vanished(tmp_path: Path) -> None:
    memory = StubMemory(indexed={"deleted.py": "deadbeef"})
    report = await IngestionPipeline(memory).index_repo(tmp_path, "acme/shop")  # type: ignore[arg-type]

    assert report.files_removed == 1
    assert memory.deleted == [("deleted.py", 0)]
