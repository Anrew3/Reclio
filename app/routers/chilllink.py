"""ChillLink Protocol endpoints: /manifest and /feeds.

Both endpoints accept the query parameters documented below. None of
them ever return 5xx — if anything fails we degrade to a valid (if
generic) response.

Query parameters
----------------
user_id       Member UUID as assigned by Reclio. Chillio forwards this
              after the user sets up the addon. Primary key for
              personalization.
username      Trakt username. Used as a fallback lookup when user_id
              isn't set yet (e.g. first install on a second device).
session_id    Opaque client identifier. Stored with the activity ping
              so we can later distinguish "one family member hitting
              the API 30x/day" from "30 family members hitting 1x/day".
last_watched  Optional "movie:<tmdb_id>" or "show:<tmdb_id>" hint so
              Chillio can signal live context even before our next
              Trakt sync completes.
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
from app.services.llm import get_llm

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


def _parse_last_watched(value: str | None) -> tuple[str, int] | None:
    """Accept 'movie:123' or 'show:456'. Return (kind, tmdb_id) or None.

    Anything else (malformed, out-of-range) is silently dropped — we never
    let client input break the /feeds response.
    """
    if not value or ":" not in value:
        return None
    try:
        kind, raw_id = value.split(":", 1)
        kind = kind.strip().lower()
        if kind not in ("movie", "show"):
            return None
        tmdb_id = int(raw_id.strip())
        if tmdb_id <= 0 or tmdb_id > 10_000_000:
            return None
        return kind, tmdb_id
    except (ValueError, AttributeError):
        return None


@router.get("/feeds")
async def feeds(
    user_id: str | None = Query(default=None, description="Reclio member UUID"),
    username: str | None = Query(
        default=None, max_length=64, description="Trakt username fallback"
    ),
    session_id: str | None = Query(
        default=None, max_length=128, description="Chillio client/device identifier"
    ),
    last_watched: str | None = Query(
        default=None,
        max_length=40,
        description="Live hint: 'movie:<tmdb_id>' or 'show:<tmdb_id>'",
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return the 22-feed personalized response. Always succeeds."""
    import json

    user: User | None = None
    taste: TasteCache | None = None
    byw_titles: dict[str, str] = {}

    try:
        # 1. Resolve user: by ID first, then by username as fallback.
        if user_id:
            user = await session.get(User, user_id)
        if user is None and username:
            safe_username = username.strip().lower()
            if safe_username:
                q = select(User).where(User.trakt_username == safe_username)
                user = (await session.execute(q)).scalar_one_or_none()

        if user is not None:
            taste = await session.get(TasteCache, user.id)
            user.last_seen = datetime.utcnow()
            try:
                from app.services.activity import record_feed_hit

                await record_feed_hit(session, user, commit=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("activity ping skipped: %s", exc)
            await session.commit()

            if session_id:
                # Logged only — stored tracking is intentionally not yet
                # persisted per-session (no schema bloat until we need it).
                logger.debug(
                    "feeds: user=%s session=%s ua-hit", user.id, session_id[:32]
                )

        # 2. Apply the optional live-context hint. Lets Chillio tell us
        #    "the user just finished Interstellar" without waiting for
        #    the next Trakt sync to land.
        live_hint = _parse_last_watched(last_watched)
        if live_hint:
            kind, tmdb_id = live_hint
            if taste is None:
                taste = TasteCache(user_id=user.id) if user else None
            if taste is not None:
                if kind == "movie":
                    taste.last_watched_movie_tmdb_id = tmdb_id
                else:
                    taste.last_watched_show_tmdb_id = tmdb_id

        # 3. Pre-generate BYW titles via the configured LLM (or fall back).
        if taste is not None:
            try:
                llm = get_llm()
                if taste.last_watched_movie_title:
                    byw_titles["movie"] = await llm.generate_byw_title(
                        taste.last_watched_movie_title, "movie"
                    )
                if taste.last_watched_show_title:
                    byw_titles["show"] = await llm.generate_byw_title(
                        taste.last_watched_show_title, "show"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("LLM BYW title generation skipped: %s", exc)
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
