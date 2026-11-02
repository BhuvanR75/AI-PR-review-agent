"""Embeddings.

`FakeEmbedder` is not a stub that returns noise. It is a signed random projection of
a bag of tokens: shared identifiers push two vectors toward each other, so retrieval
tests exercise real ranking behaviour with no API key and no network. That is what
makes the offline spine worth testing rather than merely running.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from backend.reliability.retry import RetryPolicy, call_with_retry
from backend.settings import Settings, get_settings

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, with snake_case and camelCase split so that
    `charge_customer` and `chargeCustomer` share the tokens `charge` and `customer`."""
    raw = text.replace("_", " ")
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    tokens: list[str] = []
    for match in _TOKEN_RE.findall(raw):
        lowered = match.lower()
        if len(lowered) > 1:
            tokens.append(lowered)
    return tokens


class Embedder(Protocol):
    dimension: int
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class FakeEmbedder:
    """Deterministic, offline, and semantically meaningful at the lexical level."""

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.name = f"fake-hash-{dimension}"

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class OpenAIEmbedder:
    """text-embedding-3-large, asked for `dimension` output dims via Matryoshka
    truncation. 256 keeps the index small while staying useful for code."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.dimension = self.settings.embedding_dim
        self.name = self.settings.embedding_model
        self._policy = RetryPolicy(
            attempts=self.settings.llm_max_retries,
            base_delay=0.5,
            max_delay=10.0,
            label="embeddings",
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        settings = self.settings

        async def _call() -> list[list[float]]:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                response = await client.post(
                    f"{settings.openai_base_url.rstrip('/')}/embeddings",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.embedding_model,
                        "input": texts,
                        "dimensions": settings.embedding_dim,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            rows = sorted(payload["data"], key=lambda d: d["index"])
            return [[float(x) for x in row["embedding"]] for row in rows]

        return await call_with_retry(_call, policy=self._policy)


def get_embedder(settings: Settings | None = None) -> Embedder:
    resolved = settings or get_settings()
    if resolved.embedding_provider == "openai":
        return OpenAIEmbedder(resolved)
    return FakeEmbedder(resolved.embedding_dim)
