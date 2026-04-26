"""Vector embedding generation. Picks the backend automatically based
on LLM_PROVIDER (the same env var that drives the chat LLM).

  LLM_PROVIDER=ollama → POST {OLLAMA_HOST}/api/embeddings  (768-d nomic-embed-text)
  LLM_PROVIDER=openai → text-embedding-3-small             (1536-d, batch up to 2048)
  LLM_PROVIDER=claude → local sentence-transformers MiniLM (384-d, ~250 MB resident)
  LLM_PROVIDER=none   → NullProvider (returns [], callers handle gracefully)

No new env var. The previous module's public helpers (`build_embedding_text`,
`embed_text`, `embed_texts`) are preserved so existing call sites in
content_sync.py and vector_store.py keep working — they now route
through the provider abstraction internally.

Failure semantics: every provider returns [] on error rather than
raising. content_sync handles "skip this batch, retry next pass".
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Iterable

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================
# Provider abstraction (v1.5)
# ============================================================


class EmbeddingProvider(ABC):
    name: str = "base"
    dim: int = 0

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return one float vector per input text. Empty list on failure."""

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class NullEmbeddingProvider(EmbeddingProvider):
    name = "none"
    dim = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return []


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Same Ollama instance as the LLM. POST /api/embeddings per call."""
    name = "ollama"
    dim = 768  # nomic-embed-text

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(4)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0, connect=5.0),
            )
        return self._client

    async def _embed_one(self, text: str) -> list[float]:
        async with self._sem:
            try:
                client = await self._get_client()
                resp = await client.post(
                    "/api/embeddings", json={"model": self.model, "prompt": text},
                )
                resp.raise_for_status()
                emb = resp.json().get("embedding") or []
                return list(emb)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Ollama embeddings failed: %s", exc)
                return []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = await asyncio.gather(*[self._embed_one(t) for t in texts])
        if any(not v for v in results):
            return []
        return results

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI text-embedding-3-small. Native batch up to 2048 inputs."""
    name = "openai"
    dim = 1536
    _API = "https://api.openai.com/v1/embeddings"
    _MODEL = "text-embedding-3-small"

    def __init__(self, api_key: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=5.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
            )
        return self._client

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            return []
        try:
            client = await self._get_client()
            resp = await client.post(self._API, json={
                "model": self._MODEL,
                "input": texts,
            })
            resp.raise_for_status()
            items = (resp.json().get("data") or [])
            items.sort(key=lambda x: x.get("index", 0))
            return [list(item.get("embedding") or []) for item in items]
        except Exception as exc:  # noqa: BLE001
            logger.debug("OpenAI embeddings failed: %s", exc)
            return []

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class LocalEmbeddingProvider(EmbeddingProvider):
    """sentence-transformers/all-MiniLM-L6-v2, 384-d.

    Used for LLM_PROVIDER=claude (Anthropic ships no embeddings) and
    when LLM_PROVIDER is unset. Model loads lazily — adds ~250 MB
    resident, downloaded once to ~/.cache/huggingface.
    """
    name = "local"
    dim = 384
    _MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
                self._model = await asyncio.to_thread(SentenceTransformer, self._MODEL_NAME)
                logger.info("local embeddings: loaded %s (%dd)", self._MODEL_NAME, self.dim)
                return self._model
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "local embeddings unavailable (sentence-transformers missing?): %s",
                    exc,
                )
                return None

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = await self._ensure_model()
        if model is None:
            return []
        try:
            vectors = await asyncio.to_thread(
                model.encode, texts, batch_size=32, show_progress_bar=False,
                convert_to_numpy=True,
            )
            return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vectors]
        except Exception as exc:  # noqa: BLE001
            logger.debug("local embeddings failed: %s", exc)
            return []


# ---- Factory ----

_provider: EmbeddingProvider | None = None


def _build_provider() -> EmbeddingProvider:
    """Pick a provider based on LLM_PROVIDER. Same env var as the LLM."""
    settings = get_settings()
    llm = settings.llm_provider
    if llm == "none":
        return NullEmbeddingProvider()
    if llm == "ollama":
        return OllamaEmbeddingProvider()
    if llm == "openai":
        if not settings.openai_api_key:
            logger.warning(
                "embeddings: LLM_PROVIDER=openai but OPENAI_API_KEY missing; "
                "falling back to local sentence-transformers."
            )
            return LocalEmbeddingProvider()
        return OpenAIEmbeddingProvider()
    # claude (and unknown) → local sentence-transformers
    return LocalEmbeddingProvider()


def get_embeddings_provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


# ============================================================
# Backward-compatible helpers (used by content_sync.py + vector_store.py)
# ============================================================


def build_embedding_text(
    title: str,
    overview: str | None,
    genres: Iterable[dict] | None,
    cast: Iterable[dict] | None,
    keywords: Iterable[dict] | None,
) -> str:
    """Compose the text we feed to the embedder. Stable across providers."""
    genre_names = ", ".join(g.get("name", "") for g in (genres or []) if g.get("name"))
    cast_names = ", ".join(c.get("name", "") for c in (cast or []) if c.get("name"))
    keyword_names = ", ".join(k.get("name", "") for k in (keywords or []) if k.get("name"))
    parts = [f"{title}."]
    if overview:
        parts.append(overview)
    if genre_names:
        parts.append(f"Genres: {genre_names}.")
    if cast_names:
        parts.append(f"Cast: {cast_names}.")
    if keyword_names:
        parts.append(f"Keywords: {keyword_names}.")
    return " ".join(parts)


async def embed_text(text: str) -> list[float]:
    """Single-string convenience. Use embed_texts for batches — it's
    much cheaper for the OpenAI provider (one HTTP call) and the
    Ollama provider (parallel up to 4)."""
    if not text:
        return []
    out = await get_embeddings_provider().embed_batch([text])
    return out[0] if out else []


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed — preferred entry point for content_sync."""
    if not texts:
        return []
    return await get_embeddings_provider().embed_batch(texts)
