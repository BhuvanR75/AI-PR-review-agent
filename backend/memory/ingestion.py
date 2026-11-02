"""Ingestion: turn a repository into embedded code chunks.

Two properties matter more than sophistication here.

Determinism: chunking is a pure function of file content, so re-indexing the same
tree produces the same chunk indexes and the upsert is a no-op. That is what makes
`repo_file_index` a meaningful freshness signal rather than a guess.

Incrementality: a file whose content hash is unchanged is skipped entirely, so a
weekly re-index costs the changed files, not the repository. Stale chunks are the
grounding failure the study warns about at L4 -- a refactored function whose old
chunk is still retrieved -- and hash comparison is what closes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.memory.code_memory import ChunkInput, CodeMemory, IndexedFile

logger = logging.getLogger(__name__)

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".next",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".local",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)

TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".sql",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".sh",
        ".css",
        ".html",
    }
)

#: Patterns that start a top-level symbol worth its own chunk. Deliberately shallow:
#: a real parser per language is a large dependency, and retrieval quality depends
#: far more on the chunk carrying its own definition line than on perfect AST edges.
SYMBOL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:async\s+)?def\s+(\w+)"), "def"),
    (re.compile(r"^class\s+(\w+)"), "class"),
    (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
    (re.compile(r"^export\s+(?:const|let|var)\s+(\w+)"), "binding"),
)

MAX_FILE_BYTES = 400_000


def estimate_tokens(text: str) -> int:
    """Rough, and honest about it: ~4 characters per token for code."""
    return max(1, len(text) // 4)


def find_symbol_boundaries(lines: list[str]) -> list[tuple[int, str]]:
    boundaries: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if line[:1] in {" ", "\t"}:
            continue  # nested definitions belong to their parent's chunk
        for pattern, _kind in SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                boundaries.append((index, match.group(1)))
                break
    return boundaries


def chunk_source(
    path: str,
    content: str,
    *,
    max_lines: int = 120,
    max_chars: int = 4000,
) -> list[ChunkInput]:
    """Split one file into chunks. Pure function of (path, content)."""
    if not content.strip():
        return []

    lines = content.splitlines()
    boundaries = find_symbol_boundaries(lines)

    segments: list[tuple[str | None, list[str]]] = []
    if not boundaries:
        segments.append((None, lines))
    else:
        if boundaries[0][0] > 0:
            segments.append((None, lines[: boundaries[0][0]]))
        for position, (start, symbol) in enumerate(boundaries):
            end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
            segments.append((symbol, lines[start:end]))

    chunks: list[ChunkInput] = []
    chunk_index = 0
    for segment_symbol, segment in segments:
        for piece in _split_segment(segment, max_lines=max_lines, max_chars=max_chars):
            body = "\n".join(piece).strip("\n")
            if not body.strip():
                continue
            chunks.append(
                ChunkInput(
                    path=path,
                    chunk_index=chunk_index,
                    content=body,
                    symbol=segment_symbol,
                    token_count=estimate_tokens(body),
                )
            )
            chunk_index += 1
    return chunks


def _split_segment(segment: list[str], *, max_lines: int, max_chars: int) -> list[list[str]]:
    pieces: list[list[str]] = []
    current: list[str] = []
    char_count = 0
    for line in segment:
        if current and (len(current) >= max_lines or char_count + len(line) > max_chars):
            pieces.append(current)
            current = []
            char_count = 0
        current.append(line)
        char_count += len(line) + 1
    if current:
        pieces.append(current)
    return pieces


def iter_candidate_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if path.name.startswith("."):
            continue
        found.append(path)
    return found


@dataclass
class IngestionReport:
    repo: str
    root: str
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    chunks_written: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"repo={self.repo}",
            f"scanned={self.files_scanned}",
            f"indexed={self.files_indexed}",
            f"skipped={self.files_skipped}",
            f"removed={self.files_removed}",
            f"chunks={self.chunks_written}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


class IngestionPipeline:
    def __init__(self, memory: CodeMemory) -> None:
        self.memory = memory

    async def index_repo(
        self,
        root: Path,
        repo: str,
        *,
        force: bool = False,
        max_workers: int = 8,
    ) -> IngestionReport:
        report = IngestionReport(repo=repo, root=str(root))
        indexed = {} if force else await self.memory.indexed_files(repo)
        candidates = await asyncio.to_thread(iter_candidate_files, root)
        report.files_scanned = len(candidates)

        seen_paths: set[str] = set()
        pending_chunks: list[ChunkInput] = []
        touched: list[IndexedFile] = []

        for path in candidates:
            rel = path.relative_to(root).as_posix()
            seen_paths.add(rel)
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                report.errors.append(f"{rel}: {exc}")
                continue
            if len(raw) > MAX_FILE_BYTES or b"\x00" in raw[:1024]:
                report.files_skipped += 1
                continue

            digest = hashlib.sha256(raw).hexdigest()
            if indexed.get(rel) == digest:
                report.files_skipped += 1
                continue

            content = raw.decode("utf-8", errors="ignore")
            chunks = chunk_source(rel, content)
            if not chunks:
                report.files_skipped += 1
                continue
            pending_chunks.extend(chunks)
            touched.append(IndexedFile(path=rel, content_hash=digest, size_bytes=len(raw)))
            report.files_indexed += 1

        if pending_chunks:
            report.chunks_written = await self.memory.upsert_chunks(repo, pending_chunks)
            await self.memory.record_files(repo, touched)
            for entry in touched:
                # A file that shrank keeps no orphaned tail chunks.
                count = sum(1 for c in pending_chunks if c.path == entry.path)
                await self.memory.delete_file_chunks(repo, entry.path, keep=count)

        stale = [p for p in indexed if p not in seen_paths]
        for rel in stale:
            await self.memory.delete_file_chunks(repo, rel, keep=0)
            await self.memory.pool.execute(
                "DELETE FROM repo_file_index WHERE repo = $1 AND path = $2", repo, rel
            )
            report.files_removed += 1

        return report
