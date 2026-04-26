"""Daily content sync: TMDB → embeddings → ChromaDB + Recombee + catalog table.

v1.5: also persists embedding bytes + dim + model + source_hash to the
content_catalog row so the new SQLite-backed similarity service can
do cosine queries without re-embedding. ChromaDB upsert is preserved
for backward compat.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select

from app.database import session_scope
from app.models.content import ContentCatalog
from app.services.embeddings import build_embedding_text, embed_text, get_embeddings_provider
from app.services.recombee import get_recombee
from app.services.tmdb import get_tmdb
from app.services.vector_store import upsert as vs_upsert

logger = logging.getLogger(__name__)

# Per-run caps to keep the job's wall time bounded on low-traffic deploys
_MAX_NEW_PER_RUN = 300


def _embedding_source_hash(text: str) -> str:
    """Stable 16-char fingerprint of the embedding input. Lets us skip
    re-embedding on subsequent passes when the input hasn't changed."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack a python list[float] into a numpy float32 byte buffer.

    NumPy is a soft dependency: if it's missing we fall back to Python's
    struct.pack — slightly slower but always available.
    """
    try:
        import numpy as np
        return np.array(vec, dtype=np.float32).tobytes()
    except ImportError:
        import struct
        return struct.pack(f"<{len(vec)}f", *vec)


async def _gather_candidate_items() -> dict[str, dict]:
    """Fetch TMDB discovery endpoints. Returns {tmdb_id_key: tmdb_summary}."""
    tmdb = get_tmdb()
    results = await asyncio.gather(
        tmdb.get_popular_movies(),
        tmdb.get_top_rated_movies(),
        tmdb.get_trending_movies(),
        tmdb.get_now_playing(),
        tmdb.get_popular_shows(),
        tmdb.get_top_rated_shows(),
        tmdb.get_trending_shows(),
        tmdb.get_on_the_air(),
        return_exceptions=True,
    )
    movie_results = [r for r in results[:4] if isinstance(r, list)]
    show_results = [r for r in results[4:] if isinstance(r, list)]

    bucket: dict[str, dict] = {}
    for lst in movie_results:
        for item in lst:
            tmdb_id = item.get("id")
            if tmdb_id:
                key = f"movie_{tmdb_id}"
                if key not in bucket:
                    bucket[key] = {"media_type": "movie", "summary": item}
    for lst in show_results:
        for item in lst:
            tmdb_id = item.get("id")
            if tmdb_id:
                key = f"tv_{tmdb_id}"
                if key not in bucket:
                    bucket[key] = {"media_type": "tv", "summary": item}
    return bucket


async def _sync_item(
    key: str, media_type: str, summary: dict
) -> tuple[ContentCatalog, dict[str, Any]] | None:
    """Enrich + embed one TMDB item.

    Returns (catalog_row, recombee_properties) so the caller can batch-push
    Recombee and persist catalog rows efficiently. Returns None on failure.
    """
    tmdb = get_tmdb()
    tmdb_id = summary.get("id")
    if not tmdb_id:
        return None

    try:
        if media_type == "movie":
            full = await tmdb.get_movie(tmdb_id)
        else:
            full = await tmdb.get_show(tmdb_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("TMDB details fetch failed for %s: %s", key, exc)
        return None

    if not full:
        return None

    title = full.get("title") or full.get("name") or ""
    overview = full.get("overview")
    genres = full.get("genres") or []
    credits = (full.get("credits") or {})
    cast = (credits.get("cast") or [])[:5]
    crew = credits.get("crew") or []
    director = None
    if media_type == "movie":
        for c in crew:
            if c.get("job") == "Director":
                director = c.get("name")
                break
    keywords_container = full.get("keywords") or {}
    keywords = keywords_container.get("keywords") or keywords_container.get("results") or []
    date_field = full.get("release_date") if media_type == "movie" else full.get("first_air_date")
    year = None
    if date_field:
        try:
            year = int(date_field.split("-")[0])
        except (ValueError, IndexError):
            year = None

    # Embedding (provider follows LLM_PROVIDER automatically)
    text = build_embedding_text(title, overview, genres, cast, keywords)
    source_hash = _embedding_source_hash(text)
    embedding: list[float] = []
    try:
        embedding = await embed_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding failed for %s: %s", key, exc)
        embedding = []

    metadata = {
        "title": title,
        "media_type": media_type,
        "year": year,
        "vote_average": full.get("vote_average"),
        "popularity": full.get("popularity"),
    }

    if embedding:
        try:
            await vs_upsert(key, embedding, metadata, document=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChromaDB upsert failed for %s: %s", key, exc)

    recombee_props = {
        "title": title,
        "overview": overview,
        "genres": [g.get("name") for g in genres if g.get("name")],
        "year": year,
        "vote_average": float(full.get("vote_average") or 0.0),
        "popularity": float(full.get("popularity") or 0.0),
        "media_type": media_type,
        "cast": [c.get("name") for c in cast if c.get("name")],
        "director": director,
    }

    row = ContentCatalog(
        tmdb_id=key,
        media_type=media_type,
        title=title,
        overview=overview,
        genres=[{"id": g.get("id"), "name": g.get("name")} for g in genres],
        cast=[{"id": c.get("id"), "name": c.get("name")} for c in cast],
        director=director,
        keywords=[{"id": k.get("id"), "name": k.get("name")} for k in keywords[:10]],
        year=year,
        vote_average=full.get("vote_average"),
        popularity=full.get("popularity"),
        embedding_stored=bool(embedding),
        recombee_synced=False,  # flipped after batch push succeeds
        last_updated=datetime.utcnow(),
    )
    # v1.5: also persist embedding to SQLite columns for the new
    # similarity service. Skip if the embedder returned [] (provider
    # disabled or transient failure — re-tries next pass).
    if embedding:
        provider = get_embeddings_provider()
        row.embedding = _pack_embedding(embedding)
        row.embedding_dim = len(embedding)
        row.embedding_model = provider.name
        row.embedding_source_hash = source_hash
        row.embedding_at = datetime.utcnow()
    return row, recombee_props


async def run_content_sync() -> dict[str, int]:
    """Main entrypoint for the scheduled job. Returns summary stats."""
    logger.info("content_sync: starting")
    stats = {
        "fetched": 0, "new": 0, "errors": 0,
        "recombee_sent": 0, "recombee_succeeded": 0, "recombee_failed": 0,
    }
    try:
        candidates = await _gather_candidate_items()
        stats["fetched"] = len(candidates)
    except Exception as exc:  # noqa: BLE001
        logger.exception("content_sync: gather failed: %s", exc)
        return stats

    async with session_scope() as session:
        existing_ids = set()
        result = await session.execute(select(ContentCatalog.tmdb_id))
        for row in result.scalars():
            existing_ids.add(row)

    new_keys = [k for k in candidates if k not in existing_ids]
    new_keys = new_keys[:_MAX_NEW_PER_RUN]

    sem = asyncio.Semaphore(4)
    catalog_rows: list[ContentCatalog] = []
    recombee_items: list[tuple[str, dict[str, Any]]] = []

    async def _one(key: str):
        media_type = candidates[key]["media_type"]
        summary = candidates[key]["summary"]
        async with sem:
            try:
                result = await _sync_item(key, media_type, summary)
                if result is not None:
                    row, props = result
                    catalog_rows.append(row)
                    recombee_items.append((key, props))
                    stats["new"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("content_sync: item %s failed: %s", key, exc)
                stats["errors"] += 1

    await asyncio.gather(*[_one(k) for k in new_keys])

    # Batch push to Recombee
    recombee = get_recombee()
    failed_ids: set[str] = set()
    if recombee.available and recombee_items:
        logger.info("content_sync: batch pushing %d items to Recombee", len(recombee_items))
        push_stats = await recombee.upsert_items_batch(recombee_items)
        stats["recombee_sent"] = push_stats["sent"]
        stats["recombee_succeeded"] = push_stats["succeeded"]
        stats["recombee_failed"] = push_stats["failed"]
        failed_ids = push_stats.get("failed_ids", set())

    # Single batched commit of catalog rows
    # Mark every row whose Recombee push didn't fail as synced. The
    # previous "all-or-nothing" rule meant a single failed item left the
    # entire batch perma-unsynced (because they're now in the catalog and
    # never re-attempted).
    if catalog_rows:
        for row in catalog_rows:
            if row.tmdb_id not in failed_ids:
                row.recombee_synced = True
        async with session_scope() as session:
            session.add_all(catalog_rows)

    logger.info("content_sync: done stats=%s", stats)
    return stats
