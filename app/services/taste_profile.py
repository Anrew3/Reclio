"""Build and cache a user's taste profile from Trakt data + TMDB metadata."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.taste_cache import TasteCache
from app.services.tmdb import get_tmdb
from app.services.trakt import get_trakt
from app.utils.crypto import decrypt

logger = logging.getLogger(__name__)


def _rating_to_weight(rating: int | float) -> float:
    """Map Trakt's 1-10 rating to a genre weight in [-0.5, 1.0].

    rating 10 → 1.0, rating 5 → 0.0, rating 1 → -0.5.
    """
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return 0.0
    if r >= 5:
        return (r - 5) / 5.0  # 5→0, 10→1
    return (r - 5) / 8.0  # 1→-0.5, 5→0


def _normalize_scores(raw: dict[int, float]) -> dict[str, float]:
    if not raw:
        return {}
    # Shift to non-negative then min-max scale
    min_v = min(raw.values())
    shifted = {k: v - min_v for k, v in raw.items()}
    max_v = max(shifted.values()) or 1.0
    return {str(k): round(v / max_v, 4) for k, v in shifted.items()}


async def _fetch_tmdb_genres_for(
    media_type: str, tmdb_id: int
) -> tuple[list[dict], list[dict], str | None, int | None]:
    """Return (genres, top cast, director, year) tuple."""
    tmdb = get_tmdb()
    try:
        if media_type == "movie":
            data = await tmdb.get_movie(tmdb_id)
        else:
            data = await tmdb.get_show(tmdb_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("TMDB fetch failed for %s %s: %s", media_type, tmdb_id, exc)
        return [], [], None, None

    if not data:
        return [], [], None, None

    genres = data.get("genres", []) or []
    credits = data.get("credits", {}) or {}
    cast = (credits.get("cast") or [])[:5]
    crew = credits.get("crew") or []
    director = None
    if media_type == "movie":
        for c in crew:
            if c.get("job") == "Director":
                director = c.get("name")
                break
    year = None
    if media_type == "movie":
        rel = data.get("release_date") or ""
        if rel:
            try:
                year = int(rel.split("-")[0])
            except (ValueError, IndexError):
                year = None
    else:
        first = data.get("first_air_date") or ""
        if first:
            try:
                year = int(first.split("-")[0])
            except (ValueError, IndexError):
                year = None
    return genres, cast, director, year


async def build_taste_profile(
    session: AsyncSession,
    user_id: str,
    trakt_access_token_enc: str,
) -> TasteCache | None:
    """Compute + persist a TasteCache row for the user."""
    access_token = decrypt(trakt_access_token_enc)
    if not access_token:
        logger.warning("Unable to decrypt Trakt token for user %s", user_id)
        return None

    trakt = get_trakt()

    # Fetch ratings (movies + shows) and history in parallel
    try:
        movie_ratings, show_ratings, movie_history, show_history = await asyncio.gather(
            trakt.get_ratings(access_token, "movies"),
            trakt.get_ratings(access_token, "shows"),
            trakt.get_watch_history(access_token, limit=200, media_type="movies"),
            trakt.get_watch_history(access_token, limit=200, media_type="shows"),
            return_exceptions=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trakt fetch failed for user %s: %s", user_id, exc)
        return None

    # --- Compute genre affinities ---
    movie_genre_raw: dict[int, float] = defaultdict(float)
    movie_genre_counts: dict[int, int] = defaultdict(int)
    show_genre_raw: dict[int, float] = defaultdict(float)
    show_genre_counts: dict[int, int] = defaultdict(int)

    # Collect unique tmdb_ids we need metadata for (cap to avoid rate-limits)
    movie_items = [r for r in movie_ratings if (r.get("movie") or {}).get("ids", {}).get("tmdb")][:60]
    show_items = [r for r in show_ratings if (r.get("show") or {}).get("ids", {}).get("tmdb")][:60]

    # Gather actor/director stats from history where rating unavailable
    actor_counter: Counter[tuple[int, str]] = Counter()
    director_counter: Counter[str] = Counter()
    decade_counter: Counter[int] = Counter()

    async def _process_movie(rating_item: dict) -> None:
        movie = rating_item.get("movie") or {}
        tmdb_id = (movie.get("ids") or {}).get("tmdb")
        rating = rating_item.get("rating")
        if not tmdb_id:
            return
        weight = _rating_to_weight(rating)
        genres, cast, director, year = await _fetch_tmdb_genres_for("movie", tmdb_id)
        for g in genres:
            gid = g.get("id")
            if gid is not None:
                movie_genre_raw[gid] += weight
                movie_genre_counts[gid] += 1
        for c in cast[:3]:
            cid, name = c.get("id"), c.get("name")
            if cid and name:
                actor_counter[(cid, name)] += max(1, int(round(weight * 3)))
        if director:
            director_counter[director] += max(1, int(round(weight * 3)))
        if year:
            decade_counter[(year // 10) * 10] += 1

    async def _process_show(rating_item: dict) -> None:
        show = rating_item.get("show") or {}
        tmdb_id = (show.get("ids") or {}).get("tmdb")
        rating = rating_item.get("rating")
        if not tmdb_id:
            return
        weight = _rating_to_weight(rating)
        genres, cast, _director, year = await _fetch_tmdb_genres_for("tv", tmdb_id)
        for g in genres:
            gid = g.get("id")
            if gid is not None:
                show_genre_raw[gid] += weight
                show_genre_counts[gid] += 1
        for c in cast[:3]:
            cid, name = c.get("id"), c.get("name")
            if cid and name:
                actor_counter[(cid, name)] += max(1, int(round(weight * 3)))
        if year:
            decade_counter[(year // 10) * 10] += 1

    # Bounded parallelism to respect TMDB rate limits
    sem = asyncio.Semaphore(6)

    async def _guarded(coro):
        async with sem:
            return await coro

    await asyncio.gather(
        *[_guarded(_process_movie(m)) for m in movie_items],
        *[_guarded(_process_show(s)) for s in show_items],
        return_exceptions=True,
    )

    # Augment with history if ratings are sparse
    if not movie_items:
        for h in movie_history[:40]:
            movie = h.get("movie") or {}
            tmdb_id = (movie.get("ids") or {}).get("tmdb")
            if not tmdb_id:
                continue
            genres, _cast, _director, year = await _fetch_tmdb_genres_for("movie", tmdb_id)
            for g in genres:
                gid = g.get("id")
                if gid is not None:
                    movie_genre_raw[gid] += 0.3
                    movie_genre_counts[gid] += 1
            if year:
                decade_counter[(year // 10) * 10] += 1

    if not show_items:
        for h in show_history[:40]:
            show = h.get("show") or {}
            tmdb_id = (show.get("ids") or {}).get("tmdb")
            if not tmdb_id:
                continue
            genres, _cast, _director, year = await _fetch_tmdb_genres_for("tv", tmdb_id)
            for g in genres:
                gid = g.get("id")
                if gid is not None:
                    show_genre_raw[gid] += 0.3
                    show_genre_counts[gid] += 1
            if year:
                decade_counter[(year // 10) * 10] += 1

    movie_scores = _normalize_scores(dict(movie_genre_raw))
    show_scores = _normalize_scores(dict(show_genre_raw))

    # --- Last watched ---
    last_movie_id, last_movie_title = None, None
    for item in movie_history:
        movie = item.get("movie") or {}
        tmdb_id = (movie.get("ids") or {}).get("tmdb")
        if tmdb_id:
            last_movie_id = tmdb_id
            last_movie_title = movie.get("title")
            break

    last_show_id, last_show_title = None, None
    for item in show_history:
        show = item.get("show") or {}
        tmdb_id = (show.get("ids") or {}).get("tmdb")
        if tmdb_id:
            last_show_id = tmdb_id
            last_show_title = show.get("title")
            break

    # --- Top actors & directors ---
    top_actors = [
        {"id": cid, "name": name}
        for (cid, name), _count in actor_counter.most_common(3)
    ]
    top_directors = [name for name, _count in director_counter.most_common(2)]

    preferred_decade = decade_counter.most_common(1)[0][0] if decade_counter else None

    # --- Persist ---
    cache = await session.get(TasteCache, user_id)
    if cache is None:
        cache = TasteCache(user_id=user_id)
        session.add(cache)

    cache.movie_genre_scores = movie_scores
    cache.show_genre_scores = show_scores
    cache.last_watched_movie_tmdb_id = last_movie_id
    cache.last_watched_movie_title = last_movie_title
    cache.last_watched_show_tmdb_id = last_show_id
    cache.last_watched_show_title = last_show_title
    cache.top_actors = top_actors
    cache.top_directors = [{"name": d} for d in top_directors]
    cache.preferred_decade = preferred_decade
    cache.total_movies_watched = len(movie_history)
    cache.total_shows_watched = len(show_history)
    cache.computed_at = datetime.utcnow()
    cache.is_stale = False

    await session.commit()
    return cache
