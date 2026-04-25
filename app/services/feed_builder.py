"""Core feed builder: produces the 10-feed ChillLink response.

Layout: 5 movie sections paired with 5 show sections —
    1/2  Recommended For You       (movies / shows)   ← Recombee/Trakt list
    3/4  Because You Watched X     (movies / shows)   ← Recombee item-to-item
    5/6  Trending                  (movies / shows)
    7/8  Top Genre You'll Love     (movies / shows)
    9/10 Hidden Gems               (movies / shows)

Recommended + Because You Watched both flow through managed Trakt lists
populated by user_sync via Recombee. Recombee's recommendItemsToUser /
recommendItemsToItem inherently skip items the user has already watched,
so neither row repeats history. Trending / Top Genre / Hidden Gems are
TMDB discover queries — broader "what's good" signals where seeing the
occasional already-watched title is acceptable. User preferences (era,
excluded genres, family-safe, discovery slider) feed into the discover
params for the latter three pairs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.services.tmdb import MOVIE_GENRES, TV_GENRES

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


def _genre_name(genre_id: int, media_type: str) -> str:
    if media_type == "movies":
        return MOVIE_GENRES.get(genre_id, "Top Picks")
    return TV_GENRES.get(genre_id, "Top Picks")


def _join_genre_ids(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids) if ids else ""


def _prefs_extra_params(
    prefs: UserPreferences | None, media_type: str
) -> str:
    """Build the extra TMDB /discover params dictated by user preferences.

    Returns a string like '&without_genres=27,53&include_adult=false' that
    can be appended to an existing parameters string. Empty string when no
    preferences apply.
    """
    if prefs is None:
        return ""
    parts: list[str] = []

    # Excluded genres → without_genres
    excluded = prefs.excluded_movie_genres if media_type == "movies" else prefs.excluded_show_genres
    if excluded:
        parts.append(f"without_genres={','.join(str(g) for g in excluded)}")

    # Family-safe → drop adult, cap movie certifications. TV doesn't have a
    # single global cert system on TMDB, so we only apply the cert floor to
    # movies; the include_adult=false flag works for both.
    if prefs.family_safe:
        parts.append("include_adult=false")
        if media_type == "movies":
            parts.append("certification_country=US&certification.lte=PG-13")

    # Era preference: only skew on the strong ends to avoid overfitting.
    # 0..30   → "I love classics" — cap release year at 2005
    # 70..100 → "only new" — floor at 5 years ago
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

    return ("&" + "&".join(parts)) if parts else ""


def _hidden_gem_thresholds(prefs: UserPreferences | None) -> tuple[int, int]:
    """Return (vote_count_min, popularity_max) for hidden-gem rows, scaled
    by discovery_level. Higher discovery → fewer minimum votes (deeper
    into the long tail) and stricter popularity cap (truer hidden gems).
    """
    if prefs is None:
        return 500, 30  # historical defaults
    level = max(0, min(100, prefs.discovery_level))
    # Linear interp: discovery 0 → (1000, 50)  ;  discovery 100 → (200, 15)
    vote_min = int(1000 + (200 - 1000) * (level / 100))
    pop_max = int(50 + (15 - 50) * (level / 100))
    return vote_min, pop_max


async def build_feeds(
    session: AsyncSession,
    user: User | None,
    taste: TasteCache | None,
    byw_titles: dict[str, str] | None = None,
    prefs: UserPreferences | None = None,
) -> list[dict[str, Any]]:
    """Build the 22-feed personalized response.

    Args:
        session: DB session (reserved for future lookups)
        user: current user or None (guest/unknown)
        taste: cached taste profile or None
        byw_titles: optional pre-generated "Because You Watched" titles
                    keyed by "movie" and "show" (from ollama.py)
        prefs: user preferences captured via the onboarding questionnaire
               or None (defaults baked into the builder still apply).
    """
    byw_titles = byw_titles or {}

    # --- Derive dynamic bits with safe defaults ---
    last_movie_id = taste.last_watched_movie_tmdb_id if taste else None
    last_movie_title = taste.last_watched_movie_title if taste else None
    last_show_id = taste.last_watched_show_tmdb_id if taste else None
    last_show_title = taste.last_watched_show_title if taste else None

    # Excluded genres also drop out of the "top genres" we use for row
    # titles — otherwise a user who excludes Horror could still see a
    # "Horror Movies You'll Love" row.
    excluded_movie = set(prefs.excluded_movie_genres or []) if prefs else set()
    excluded_show = set(prefs.excluded_show_genres or []) if prefs else set()

    movie_genre_ids = _top_genre_ids(
        taste.movie_genre_scores if taste else None, 3, exclude=excluded_movie
    )
    show_genre_ids = _top_genre_ids(
        taste.show_genre_scores if taste else None, 3, exclude=excluded_show
    )

    movie_pref_extra = _prefs_extra_params(prefs, "movies")
    show_pref_extra = _prefs_extra_params(prefs, "shows")
    gem_vote_min, gem_pop_max = _hidden_gem_thresholds(prefs)

    top_movie_genres_str = _join_genre_ids(movie_genre_ids)
    top_show_genres_str = _join_genre_ids(show_genre_ids)

    # Top individual genre for sections 12/13
    top_movie_genre_id = movie_genre_ids[0] if movie_genre_ids else 18  # Drama fallback
    top_movie_genre_name = _genre_name(top_movie_genre_id, "movies")
    top_show_genre_id = show_genre_ids[0] if show_genre_ids else 18
    top_show_genre_name = _genre_name(top_show_genre_id, "shows")

    # Second-top genre for sections 14/15
    second_movie_genre_id = movie_genre_ids[1] if len(movie_genre_ids) > 1 else 28
    second_movie_genre_name = _genre_name(second_movie_genre_id, "movies")
    second_show_genre_id = show_genre_ids[1] if len(show_genre_ids) > 1 else 35
    second_show_genre_name = _genre_name(second_show_genre_id, "shows")

    # Managed Trakt list IDs — None if user not yet connected
    rec_movies_list_id = user.trakt_rec_movies_list_id if user else None
    rec_shows_list_id = user.trakt_rec_shows_list_id if user else None
    byw_movies_list_id = user.trakt_byw_movies_list_id if user else None
    byw_shows_list_id = user.trakt_byw_shows_list_id if user else None

    feeds: list[dict[str, Any]] = []

    # --- 1 / 2  Recommended For You -------------------------------
    # Recombee → managed Trakt list. RecommendItemsToUser inherently
    # filters out interacted-with items, so this never repeats history.
    if rec_movies_list_id:
        feeds.append({
            "id": "recommended_movies",
            "title": "Recommended For You",
            "source": "trakt_list",
            "source_metadata": {"id": rec_movies_list_id},
            "content_type": "movies",
        })
    else:
        params = (
            f"with_genres={top_movie_genres_str}&sort_by=popularity.desc"
            if top_movie_genres_str
            else "sort_by=popularity.desc"
        ) + movie_pref_extra
        feeds.append({
            "id": "recommended_movies",
            "title": "Recommended For You",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/movie", "parameters": params},
            "content_type": "movies",
        })

    if rec_shows_list_id:
        feeds.append({
            "id": "recommended_shows",
            "title": "Recommended For You",
            "source": "trakt_list",
            "source_metadata": {"id": rec_shows_list_id},
            "content_type": "shows",
        })
    else:
        params = (
            f"with_genres={top_show_genres_str}&sort_by=popularity.desc"
            if top_show_genres_str
            else "sort_by=popularity.desc"
        ) + show_pref_extra
        feeds.append({
            "id": "recommended_shows",
            "title": "Recommended For You",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/tv", "parameters": params},
            "content_type": "shows",
        })

    # --- 3 / 4  Because You Watched -------------------------------
    # Recombee → managed Trakt list keyed off the most-recent watched
    # title. RecommendItemsToItem also filters out the target user's
    # interactions, so the list never includes already-watched titles.
    # The id embeds the anchor TMDB id so Chillio sees a new feed when
    # the user finishes something new.
    if byw_movies_list_id and last_movie_id and last_movie_title:
        byw_movie_title = byw_titles.get("movie") or f"Because You Watched {last_movie_title}"
        feeds.append({
            "id": f"because_watched_movie_tmdb_{last_movie_id}",
            "title": byw_movie_title,
            "source": "trakt_list",
            "source_metadata": {"id": byw_movies_list_id},
            "content_type": "movies",
        })
    elif last_movie_id and last_movie_title:
        # Fallback: TMDB recs (may include already-watched). Better than
        # nothing while the BYW list is being populated by user_sync.
        byw_movie_title = byw_titles.get("movie") or f"Because You Watched {last_movie_title}"
        feeds.append({
            "id": f"because_watched_movie_tmdb_{last_movie_id}",
            "title": byw_movie_title,
            "source": "tmdb_query",
            "source_metadata": {
                "path": f"/movie/{last_movie_id}/recommendations",
                "parameters": "",
            },
            "content_type": "movies",
        })
    else:
        feeds.append({
            "id": "because_watched_movie",
            "title": "More Movies To Discover",
            "source": "tmdb_query",
            "source_metadata": {"path": "/movie/popular", "parameters": ""},
            "content_type": "movies",
        })

    if byw_shows_list_id and last_show_id and last_show_title:
        byw_show_title = byw_titles.get("show") or f"Because You Watched {last_show_title}"
        feeds.append({
            "id": f"because_watched_show_tmdb_{last_show_id}",
            "title": byw_show_title,
            "source": "trakt_list",
            "source_metadata": {"id": byw_shows_list_id},
            "content_type": "shows",
        })
    elif last_show_id and last_show_title:
        byw_show_title = byw_titles.get("show") or f"Because You Watched {last_show_title}"
        feeds.append({
            "id": f"because_watched_show_tmdb_{last_show_id}",
            "title": byw_show_title,
            "source": "tmdb_query",
            "source_metadata": {
                "path": f"/tv/{last_show_id}/recommendations",
                "parameters": "",
            },
            "content_type": "shows",
        })
    else:
        feeds.append({
            "id": "because_watched_show",
            "title": "More Shows To Discover",
            "source": "tmdb_query",
            "source_metadata": {"path": "/tv/popular", "parameters": ""},
            "content_type": "shows",
        })

    # --- 5 / 6  Trending ------------------------------------------
    feeds.append({
        "id": "trending_movies",
        "title": "Trending Movies",
        "source": "tmdb_query",
        "source_metadata": {"path": "/trending/movie/week", "parameters": ""},
        "content_type": "movies",
    })
    feeds.append({
        "id": "trending_shows",
        "title": "Trending Shows",
        "source": "tmdb_query",
        "source_metadata": {"path": "/trending/tv/week", "parameters": ""},
        "content_type": "shows",
    })

    # --- 7 / 8  Top Genre You'll Love -----------------------------
    feeds.append({
        "id": "top_genre_movies",
        "title": f"{top_movie_genre_name} Movies You'll Love",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/movie",
            "parameters": (
                f"with_genres={top_movie_genre_id}"
                f"&sort_by=vote_average.desc&vote_count.gte=300"
                f"{movie_pref_extra}"
            ),
        },
        "content_type": "movies",
    })
    feeds.append({
        "id": "top_genre_shows",
        "title": f"{top_show_genre_name} Shows You'll Love",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/tv",
            "parameters": (
                f"with_genres={top_show_genre_id}"
                f"&sort_by=vote_average.desc&vote_count.gte=100"
                f"{show_pref_extra}"
            ),
        },
        "content_type": "shows",
    })

    # --- 9 / 10  Hidden Gems --------------------------------------
    # Thresholds scale with discovery_level pref. TV uses ~0.5× of
    # movie thresholds because the TMDB TV catalog is smaller.
    show_gem_votes = max(50, gem_vote_min // 2)
    show_gem_pop = max(10, int(gem_pop_max * 0.7))
    feeds.append({
        "id": "hidden_gems_movies",
        "title": "Hidden Gem Movies",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/movie",
            "parameters": (
                f"vote_average.gte=7.5&vote_count.gte={gem_vote_min}"
                f"&popularity.lte={gem_pop_max}&sort_by=vote_average.desc"
                f"{movie_pref_extra}"
            ),
        },
        "content_type": "movies",
    })
    feeds.append({
        "id": "hidden_gems_shows",
        "title": "Hidden Gem Shows",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/tv",
            "parameters": (
                f"vote_average.gte=7.5&vote_count.gte={show_gem_votes}"
                f"&popularity.lte={show_gem_pop}&sort_by=vote_average.desc"
                f"{show_pref_extra}"
            ),
        },
        "content_type": "shows",
    })

    # The "second_movie_genre_*" / "second_show_genre_*" locals are
    # intentionally unused now — kept derived above so future expansion
    # back to a 12-feed layout (continue/watchlist/etc.) doesn't need
    # to recompute them.
    _ = (second_movie_genre_id, second_movie_genre_name,
         second_show_genre_id, second_show_genre_name)

    return feeds
