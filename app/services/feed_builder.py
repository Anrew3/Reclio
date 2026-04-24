"""Core feed builder: produces the 22-feed ChillLink response.

The order of feeds is fixed. For each slot we either inject dynamic
values from the user's taste profile, or fall back to sensible TMDB
defaults so every user (new or missing profile) still gets a complete
response.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.taste_cache import TasteCache
from app.models.user import User
from app.services.tmdb import MOVIE_GENRES, TV_GENRES

logger = logging.getLogger(__name__)


def _top_genre_ids(scores: dict | None, k: int = 3) -> list[int]:
    if not scores:
        return []
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out: list[int] = []
    for gid, _score in ordered[:k]:
        try:
            out.append(int(gid))
        except (TypeError, ValueError):
            continue
    return out


def _genre_name(genre_id: int, media_type: str) -> str:
    if media_type == "movies":
        return MOVIE_GENRES.get(genre_id, "Top Picks")
    return TV_GENRES.get(genre_id, "Top Picks")


def _join_genre_ids(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids) if ids else ""


async def build_feeds(
    session: AsyncSession,
    user: User | None,
    taste: TasteCache | None,
    byw_titles: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build the 22-feed personalized response.

    Args:
        session: DB session (reserved for future lookups)
        user: current user or None (guest/unknown)
        taste: cached taste profile or None
        byw_titles: optional pre-generated "Because You Watched" titles
                    keyed by "movie" and "show" (from ollama.py)
    """
    byw_titles = byw_titles or {}

    # --- Derive dynamic bits with safe defaults ---
    last_movie_id = taste.last_watched_movie_tmdb_id if taste else None
    last_movie_title = taste.last_watched_movie_title if taste else None
    last_show_id = taste.last_watched_show_tmdb_id if taste else None
    last_show_title = taste.last_watched_show_title if taste else None

    movie_genre_ids = _top_genre_ids(taste.movie_genre_scores if taste else None, 3)
    show_genre_ids = _top_genre_ids(taste.show_genre_scores if taste else None, 3)

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
    watchprogress_list_id = user.trakt_watchprogress_list_id if user else None
    watchlist_id = user.trakt_watchlist_id if user else None

    feeds: list[dict[str, Any]] = []

    # 1 - Continue Watching (Trakt in-progress)
    if watchprogress_list_id:
        feeds.append({
            "id": "continue_watching",
            "title": "Continue Watching",
            "source": "trakt_list",
            "source_metadata": {"id": watchprogress_list_id},
            "content_type": "all",
        })
    else:
        # Fallback to popular for unconnected users
        feeds.append({
            "id": "continue_watching",
            "title": "Keep Watching",
            "source": "tmdb_query",
            "source_metadata": {"path": "/trending/all/day", "parameters": ""},
            "content_type": "all",
        })

    # 2 - Recommended Movies (Recombee → managed Trakt list)
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
        )
        feeds.append({
            "id": "recommended_movies",
            "title": "Recommended For You",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/movie", "parameters": params},
            "content_type": "movies",
        })

    # 3 - Recommended Shows
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
        )
        feeds.append({
            "id": "recommended_shows",
            "title": "Recommended For You",
            "source": "tmdb_query",
            "source_metadata": {"path": "/discover/tv", "parameters": params},
            "content_type": "shows",
        })

    # 4 - Because You Watched [Last Movie]
    # ID embeds the movie TMDB id so Chillio treats a new watched movie
    # as a new feed rather than overwriting the old one. Title is
    # presentation-only (LLM-generated or f-string fallback).
    if last_movie_id and last_movie_title:
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
            "id": "because_watched_movie",  # stable fallback id for unknown-user case
            "title": "More Movies To Discover",
            "source": "tmdb_query",
            "source_metadata": {"path": "/movie/popular", "parameters": ""},
            "content_type": "movies",
        })

    # 5 - Because You Watched [Last Show]
    if last_show_id and last_show_title:
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
            "id": "because_watched_show",  # stable fallback id
            "title": "More Shows To Discover",
            "source": "tmdb_query",
            "source_metadata": {"path": "/tv/popular", "parameters": ""},
            "content_type": "shows",
        })

    # 6 - Similar Movies
    sim_movie_params = (
        f"with_genres={top_movie_genres_str}&sort_by=vote_average.desc&vote_count.gte=200"
        if top_movie_genres_str
        else "sort_by=vote_average.desc&vote_count.gte=200"
    )
    feeds.append({
        "id": "similar_movies",
        "title": "Similar To Movies You've Watched",
        "source": "tmdb_query",
        "source_metadata": {"path": "/discover/movie", "parameters": sim_movie_params},
        "content_type": "movies",
    })

    # 7 - Similar Shows
    sim_show_params = (
        f"with_genres={top_show_genres_str}&sort_by=vote_average.desc&vote_count.gte=100"
        if top_show_genres_str
        else "sort_by=vote_average.desc&vote_count.gte=100"
    )
    feeds.append({
        "id": "similar_shows",
        "title": "Similar To Shows You've Watched",
        "source": "tmdb_query",
        "source_metadata": {"path": "/discover/tv", "parameters": sim_show_params},
        "content_type": "shows",
    })

    # 8 - Trending Movies
    feeds.append({
        "id": "trending_movies",
        "title": "Trending Movies",
        "source": "tmdb_query",
        "source_metadata": {"path": "/trending/movie/week", "parameters": ""},
        "content_type": "movies",
    })

    # 9 - Trending Shows
    feeds.append({
        "id": "trending_shows",
        "title": "Trending Shows",
        "source": "tmdb_query",
        "source_metadata": {"path": "/trending/tv/week", "parameters": ""},
        "content_type": "shows",
    })

    # 10 - New Movies For You (genre-filtered now playing)
    new_movie_params = (
        f"with_genres={top_movie_genres_str}" if top_movie_genres_str else ""
    )
    feeds.append({
        "id": "new_movies",
        "title": "New Movies For You",
        "source": "tmdb_query",
        "source_metadata": {"path": "/movie/now_playing", "parameters": new_movie_params},
        "content_type": "movies",
    })

    # 11 - New Shows For You
    new_show_params = (
        f"with_genres={top_show_genres_str}" if top_show_genres_str else ""
    )
    feeds.append({
        "id": "new_shows",
        "title": "New Shows For You",
        "source": "tmdb_query",
        "source_metadata": {"path": "/tv/on_the_air", "parameters": new_show_params},
        "content_type": "shows",
    })

    # 12 - Top Genre Movies
    feeds.append({
        "id": "top_genre_movies",
        "title": f"{top_movie_genre_name} Movies You'll Love",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/movie",
            "parameters": (
                f"with_genres={top_movie_genre_id}"
                f"&sort_by=vote_average.desc&vote_count.gte=300"
            ),
        },
        "content_type": "movies",
    })

    # 13 - Top Genre Shows
    feeds.append({
        "id": "top_genre_shows",
        "title": f"{top_show_genre_name} Shows You'll Love",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/tv",
            "parameters": (
                f"with_genres={top_show_genre_id}"
                f"&sort_by=vote_average.desc&vote_count.gte=100"
            ),
        },
        "content_type": "shows",
    })

    # 14 - Second Genre Movies
    feeds.append({
        "id": "second_genre_movies",
        "title": f"More {second_movie_genre_name} For You",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/movie",
            "parameters": f"with_genres={second_movie_genre_id}&sort_by=popularity.desc",
        },
        "content_type": "movies",
    })

    # 15 - Second Genre Shows
    feeds.append({
        "id": "second_genre_shows",
        "title": f"More {second_show_genre_name} For You",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/tv",
            "parameters": f"with_genres={second_show_genre_id}&sort_by=popularity.desc",
        },
        "content_type": "shows",
    })

    # 16 - Popular Movies
    feeds.append({
        "id": "popular_movies",
        "title": "Popular Movies",
        "source": "tmdb_query",
        "source_metadata": {"path": "/movie/popular", "parameters": ""},
        "content_type": "movies",
    })

    # 17 - Popular Shows
    feeds.append({
        "id": "popular_shows",
        "title": "Popular Shows",
        "source": "tmdb_query",
        "source_metadata": {"path": "/tv/popular", "parameters": ""},
        "content_type": "shows",
    })

    # 18 - Hidden Gem Movies
    feeds.append({
        "id": "hidden_gems_movies",
        "title": "Hidden Gem Movies",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/movie",
            "parameters": (
                "vote_average.gte=7.5&vote_count.gte=500"
                "&popularity.lte=30&sort_by=vote_average.desc"
            ),
        },
        "content_type": "movies",
    })

    # 19 - Hidden Gem Shows
    feeds.append({
        "id": "hidden_gems_shows",
        "title": "Hidden Gem Shows",
        "source": "tmdb_query",
        "source_metadata": {
            "path": "/discover/tv",
            "parameters": (
                "vote_average.gte=7.5&vote_count.gte=200"
                "&popularity.lte=20&sort_by=vote_average.desc"
            ),
        },
        "content_type": "shows",
    })

    # 20 - Critically Acclaimed Movies
    feeds.append({
        "id": "top_rated_movies",
        "title": "Critically Acclaimed Movies",
        "source": "tmdb_query",
        "source_metadata": {"path": "/movie/top_rated", "parameters": ""},
        "content_type": "movies",
    })

    # 21 - Critically Acclaimed Shows
    feeds.append({
        "id": "top_rated_shows",
        "title": "Critically Acclaimed Shows",
        "source": "tmdb_query",
        "source_metadata": {"path": "/tv/top_rated", "parameters": ""},
        "content_type": "shows",
    })

    # 22 - Watchlist
    if watchlist_id:
        feeds.append({
            "id": "watchlist",
            "title": "Your Watchlist",
            "source": "trakt_list",
            "source_metadata": {"id": watchlist_id},
            "content_type": "all",
        })
    else:
        feeds.append({
            "id": "watchlist",
            "title": "Coming Soon",
            "source": "tmdb_query",
            "source_metadata": {"path": "/movie/upcoming", "parameters": ""},
            "content_type": "movies",
        })

    return feeds
