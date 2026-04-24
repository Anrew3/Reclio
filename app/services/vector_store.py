"""ChromaDB wrapper for storing + querying content embeddings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "content_catalog"

_client = None
_collection = None
_init_lock = asyncio.Lock()


async def _init() -> None:
    global _client, _collection
    if _collection is not None:
        return
    async with _init_lock:
        if _collection is not None:
            return
        settings = get_settings()

        def _setup():
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            client = chromadb.PersistentClient(
                path=settings.chroma_persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            coll = client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            return client, coll

        _client, _collection = await asyncio.to_thread(_setup)
        logger.info("ChromaDB ready at %s", settings.chroma_persist_dir)


async def upsert(
    tmdb_id: str,
    embedding: list[float],
    metadata: dict[str, Any],
    document: str | None = None,
) -> None:
    await _init()
    meta = {k: v for k, v in metadata.items() if v is not None and not isinstance(v, (list, dict))}

    def _op():
        _collection.upsert(
            ids=[tmdb_id],
            embeddings=[embedding],
            metadatas=[meta],
            documents=[document] if document else None,
        )

    await asyncio.to_thread(_op)


async def query_similar(
    embedding: list[float],
    n: int = 20,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    await _init()

    def _op():
        return _collection.query(
            query_embeddings=[embedding],
            n_results=n,
            where=where,
        )

    result = await asyncio.to_thread(_op) or {}
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    out = []
    for i, tmdb_id in enumerate(ids):
        out.append(
            {
                "id": tmdb_id,
                "distance": distances[i] if i < len(distances) else None,
                "metadata": metadatas[i] if i < len(metadatas) else {},
            }
        )
    return out


async def contains(tmdb_id: str) -> bool:
    await _init()

    def _op():
        result = _collection.get(ids=[tmdb_id], include=[])
        return bool(result and result.get("ids"))

    return await asyncio.to_thread(_op)
