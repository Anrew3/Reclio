"""Core feed builder: produces the 2-feed ChillLink response.

Layout (v1.8 — deliberately focused):
    1  Recommended Movies   ← engine → managed Trakt list
    2  Recommended Shows    ← engine → managed Trakt list

Both rows flow through managed Trakt lists populated by user_sync via
the recommendation engine, which inherently skips items the user has
already watched or blocked — neither row repeats history. When a user
hasn't connected yet (no managed list), each row falls back to a TMDB
discover query shaped by their taste profile + onboarding preferences,
so the response is personalized-ish from the very first request.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.models.user import User

logger = logging.getLogger(__name__)


def _top_genre_ids(scores: dict | None, k: int = 3, exclude: set[int] | None = None) -> list[int]:
    if not scores:
        return []
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out: list[int] = []
    for gid, _score in ordered:
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        if exclude and gid_int in exclude:
            continue
        out.append(gid_int)
        if len(out) >= k:
            break
    return out


def _join_genre_ids(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids) if ids else ""


def _prefs_extra_params(
    prefs: UserPreferences | None,
    media_type: str,
    *,
    row_has_with_genres: bool = False,
) -> str:
    """Build extra TMDB /discover params from user preferences.

    Returns a string like '&without_genres=27&include_adult=false' that
    can be appended to an existing parameters string. Empty when no
    preferences apply.

    `row_has_with_genres` should be True for rows that already define a
    `with_genres=` in their base params. When set, we suppress the
    pacing-driven genre boost — TMDB's URL parser keeps only the LAST
    `with_genres` value, so emitting a second one would wipe the row's
    intended genre.

    Effects:
      - excluded_movie_genres / excluded_show_genres → without_genres
      - family_safe → include_adult=false (+ cert floor on movies)
      - era_preference (0..100) → primary_release_date / first_air_date
        bounds, only on strong ends (≤30 or ≥70).
      - pacing_preference (0..100) → genre boost (action genres for high,
        drama/doc for low) — skipped when row_has_with_genres.
      - runtime_preference (0..100) → with_runtime.lte / with_runtime.gte
        on the same edge bands.
    """
    if prefs is None:
        return ""
    parts: list[str] = []

    excluded = prefs.excluded_movie_genres if media_type == "movies" else prefs.excluded_show_genres
    if excluded:
        parts.append(f"without_genres={','.join(str(g) for g in excluded)}")

    if prefs.family_safe:
        parts.append("include_adult=false")
        if media_type == "movies":
            parts.append("certification_country=US&certification.lte=PG-13")

    if prefs.era_preference <= 30:
        if media_type == "movies":
            parts.append("primary_release_date.lte=2005-12-31")
        else:
            parts.append("first_air_date.lte=2005-12-31")
    elif prefs.era_preference >= 70:
        cutoff = (datetime.utcnow().year - 5)
        if media_type == "movies":
            parts.append(f"primary_release_date.gte={cutoff}-01-01")
        else:
            parts.append(f"first_air_date.gte={cutoff}-01-01")

    pacing = max(0, min(100, prefs.pacing_preference or 50))
    if not row_has_with_genres:
        if pacing >= 70:
            if media_type == "movies":
                parts.append("with_genres=28,12,53")  # Action / Adventure / Thriller
            else:
                parts.append("with_genres=10759,80")  # Action&Adventure / Crime
        elif pacing <= 30:
            parts.append("with_genres=18,99")         # Drama / Documentary

    runtime = max(0, min(100, prefs.runtime_preference or 50))
    if runtime <= 25:
        parts.append("with_runtime.lte=100")
    elif runtime >= 75:
        parts.append("with_runtime.gte=130")

    return ("&" + "&".join(parts)) if parts else ""


async def build_feeds(
    session: AsyncSession,
    user: User | None,
    taste: TasteCache | None,
    prefs: UserPreferences | None = None,
) -> list[dict[str, Any]]:
    """Build the 2-feed personalized response.

    Args:
        session: DB session (reserved for future lookups)
        user: current user or None (guest/unknown)
        taste: cached taste profile or None
        prefs: user preferences captured via the onboarding questionnaire
               or None (defaults baked into the builder still apply).
    """
    # Excluded genres drop out of the fallback-discover genre list too.
    excluded_movie = set(prefs.excluded_movie_genres or []) if prefs else set()
    excluded_show = set(prefs.excluded_show_genres or []) if prefs else set()

    movie_genre_ids = _top_genre_ids(
        taste.movie_genre_scores if taste else None, 3, exclude=excluded_movie
    )
    show_genre_ids = _top_genre_ids(
        taste.show_genre_scores if taste else None, 3, exclude=excluded_show
    )
    top_movie_genres_str = _join_genre_ids(movie_genre_ids)
    top_show_genres_str = _join_genre_ids(show_genre_ids)

    movie_pref_extra = _prefs_extra_params(prefs, "movies")
    show_pref_extra = _prefs_extra_params(prefs, "shows")
    movie_pref_safe = _prefs_extra_params(prefs, "movies", row_has_with_genres=True)
    show_pref_safe = _prefs_extra_params(prefs, "shows", row_has_with_genres=True)

    rec_movies_list_id = user.trakt_rec_movies_list_id if user else None
    rec_shows_list_id = user.trakt_rec_shows_list_id if user else None

    feeds: list[dict[str, Any]] = []

    # --- 1  Recommended Movies -------------------------------------
    if rec_movies_list_id:
        feeds.append({
            "id": "recommended_movies",
            "title": "Recommended Movies",
            "source": "trakt_list",
            "source_metadata": {"id": rec_movies_list_id},
            "content_type": "movies",
        })
    else:
        # Fallback (list not materialized yet): taste-shaped discover.
        if top_movie_genres_str:
            params = f"with_genres={top_movie_genres_str}&sort_by=popularity.desc" + movie_pref_safe
        else:
            params = "sort_by=popularity.desc" + movie_pref_extra
        feeds.append({
            "id": "recommended_movies",
            "title": "Recommended Movies",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/movie", "parameters": params},
            "content_type": "movies",
        })

    # --- 2  Recommended Shows --------------------------------------
    if rec_shows_list_id:
        feeds.append({
            "id": "recommended_shows",
            "title": "Recommended Shows",
            "source": "trakt_list",
            "source_metadata": {"id": rec_shows_list_id},
            "content_type": "shows",
        })
    else:
        if top_show_genres_str:
            params = f"with_genres={top_show_genres_str}&sort_by=popularity.desc" + show_pref_safe
        else:
            params = "sort_by=popularity.desc" + show_pref_extra
        feeds.append({
            "id": "recommended_shows",
            "title": "Recommended Shows",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/tv", "parameters": params},
            "content_type": "shows",
        })

    return feeds
