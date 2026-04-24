"""ChillLink Protocol endpoints: /manifest and /feeds.

Both endpoints accept an optional `user_id` query parameter. Chillio
forwards this on every call so we can serve per-user personalized
feeds. These endpoints must NEVER return 5xx — fall back to a valid
(possibly degraded) response instead.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.schemas.chilllink import Manifest, SupportedEndpoints
from app.services.feed_builder import build_feeds
from app.services.ollama import get_ollama

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chilllink"])


@router.get("/manifest")
async def manifest(
    user_id: str | None = Query(default=None),
) -> Response:
    """Return addon metadata. Shape is protocol-fixed."""
    mf = Manifest(
        id="reclio-recommendations",
        version="1.0.0",
        name="Reclio",
        description="Netflix-style personalized recommendations powered by your Trakt history",
        supported_endpoints=SupportedEndpoints(feeds="/feeds", streams=None),
    )
    import json

    return Response(
        content=json.dumps(mf.model_dump()),
        media_type="application/json",
    )


@router.get("/feeds")
async def feeds(
    user_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return the 22-feed personalized response. Always succeeds."""
    import json

    user: User | None = None
    taste: TasteCache | None = None
    byw_titles: dict[str, str] = {}

    try:
        if user_id:
            user = await session.get(User, user_id)
            if user is not None:
                taste = await session.get(TasteCache, user_id)
                user.last_seen = datetime.utcnow()
                await session.commit()

        if taste is not None:
            try:
                ollama = get_ollama()
                if taste.last_watched_movie_title:
                    byw_titles["movie"] = await ollama.generate_byw_title(
                        taste.last_watched_movie_title, "movie"
                    )
                if taste.last_watched_show_title:
                    byw_titles["show"] = await ollama.generate_byw_title(
                        taste.last_watched_show_title, "show"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Ollama BYW title generation skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed loading user context for feeds: %s", exc)

    try:
        feed_list = await build_feeds(session, user, taste, byw_titles)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Feed builder failed, returning minimal fallback: %s", exc)
        feed_list = _minimal_fallback_feeds()

    return Response(
        content=json.dumps({"feeds": feed_list}),
        media_type="application/json",
    )


def _minimal_fallback_feeds() -> list[dict]:
    """Absolute last-resort feeds if the builder itself errors."""
    return [
        {
            "id": "trending_movies",
            "title": "Trending Movies",
            "source": "tmdb_query",
            "source_metadata": {"path": "/trending/movie/week", "parameters": ""},
            "content_type": "movies",
        },
        {
            "id": "trending_shows",
            "title": "Trending Shows",
            "source": "tmdb_query",
            "source_metadata": {"path": "/trending/tv/week", "parameters": ""},
            "content_type": "shows",
        },
        {
            "id": "popular_movies",
            "title": "Popular Movies",
            "source": "tmdb_query",
            "source_metadata": {"path": "/movie/popular", "parameters": ""},
            "content_type": "movies",
        },
        {
            "id": "popular_shows",
            "title": "Popular Shows",
            "source": "tmdb_query",
            "source_metadata": {"path": "/tv/popular", "parameters": ""},
            "content_type": "shows",
        },
        {
            "id": "top_rated_movies",
            "title": "Critically Acclaimed Movies",
            "source": "tmdb_query",
            "source_metadata": {"path": "/movie/top_rated", "parameters": ""},
            "content_type": "movies",
        },
    ]
