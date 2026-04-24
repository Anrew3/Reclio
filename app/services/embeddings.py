"""Sentence-transformers embedding generation. Runs the model in a
thread executor to keep the async loop unblocked. Lazy-loads the model
on first use so startup stays fast.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model = None
_model_lock = asyncio.Lock()


async def _get_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model

        def _load():
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer(_MODEL_NAME)

        _model = await asyncio.to_thread(_load)
        logger.info("Embedding model loaded: %s", _MODEL_NAME)
        return _model


def build_embedding_text(
    title: str,
    overview: str | None,
    genres: Iterable[dict] | None,
    cast: Iterable[dict] | None,
    keywords: Iterable[dict] | None,
) -> str:
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


def _encode_one(model, text: str):
    return model.encode(text, show_progress_bar=False, convert_to_numpy=True)


def _encode_many(model, texts: list[str]):
    return model.encode(texts, batch_size=32, show_progress_bar=False, convert_to_numpy=True)


async def embed_text(text: str) -> list[float]:
    model = await _get_model()
    vec = await asyncio.to_thread(_encode_one, model, text)
    return vec.tolist() if hasattr(vec, "tolist") else list(vec)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = await _get_model()
    vecs = await asyncio.to_thread(_encode_many, model, texts)
    return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]
