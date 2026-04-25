"""TMDB API client with in-memory TTL caching.

Free-tier rate limit is ~40 requests per 10 seconds. We cache responses
for 6 hours by default and use a small semaphore to throttle parallel
traffic to TMDB during background sync.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)

MOVIE_GENRES: dict[int, str] = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
}

TV_GENRES: dict[int, str] = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}

_CACHE_TTL = 6 * 60 * 60  # 6 hours


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_settings().tmdb_api_key
        self._cache = TTLCache(default_ttl=_CACHE_TTL)
        self._semaphore = asyncio.Semaphore(8)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        if not self._api_key:
            logger.warning("TMDB_API_KEY not set; returning empty response for %s", path)
            return {}

        merged_params = {"api_key": self._api_key}
        if params:
            merged_params.update({k: v for k, v in params.items() if v is not None})
        cache_key = f"{path}?" + "&".join(
            f"{k}={v}" for k, v in sorted(merged_params.items()) if k != "api_key"
        )

        async def _fetch() -> dict[str, Any]:
            async with self._semaphore:
                client = await self._get_client()
                for attempt in range(3):
                    try:
                        resp = await client.get(path, params=merged_params)
                        if resp.status_code == 429:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                            await asyncio.sleep(retry_after + 0.5)
                            continue
                        resp.raise_for_status()
                        return resp.json()
                    except httpx.HTTPError as exc:
                        if attempt == 2:
                            logger.warning("TMDB GET %s failed: %s", path, exc)
                            return {}
                        await asyncio.sleep(0.5 * (attempt + 1))
                return {}

        return await self._cache.get_or_set(cache_key, _fetch, ttl=ttl)

    # --- High-level endpoints ------------------------------------

    async def get_movie(self, tmdb_id: int) -> dict:
        return await self._get(
            f"/movie/{tmdb_id}",
            params={"append_to_response": "credits,keywords"},
        )

    async def get_show(self, tmdb_id: int) -> dict:
        return await self._get(
            f"/tv/{tmdb_id}",
            params={"append_to_response": "credits,keywords"},
        )

    async def get_movie_recommendations(self, tmdb_id: int) -> list[dict]:
        data = await self._get(f"/movie/{tmdb_id}/recommendations")
        return data.get("results", []) or []

    async def get_show_recommendations(self, tmdb_id: int) -> list[dict]:
        data = await self._get(f"/tv/{tmdb_id}/recommendations")
        return data.get("results", []) or []

    async def get_movie_keywords(self, tmdb_id: int) -> list[dict]:
        data = await self._get(f"/movie/{tmdb_id}/keywords")
        return data.get("keywords", []) or []

    async def get_show_keywords(self, tmdb_id: int) -> list[dict]:
        data = await self._get(f"/tv/{tmdb_id}/keywords")
        return data.get("results", []) or []

    async def discover_movies(self, parameters: str = "") -> list[dict]:
        params = _parse_params(parameters)
        data = await self._get("/discover/movie", params=params)
        return data.get("results", []) or []

    async def discover_shows(self, parameters: str = "") -> list[dict]:
        params = _parse_params(parameters)
        data = await self._get("/discover/tv", params=params)
        return data.get("results", []) or []

    async def get_trending_movies(self) -> list[dict]:
        data = await self._get("/trending/movie/week")
        return data.get("results", []) or []

    async def get_trending_shows(self) -> list[dict]:
        data = await self._get("/trending/tv/week")
        return data.get("results", []) or []

    async def get_now_playing(self) -> list[dict]:
        data = await self._get("/movie/now_playing")
        return data.get("results", []) or []

    async def get_on_the_air(self) -> list[dict]:
        data = await self._get("/tv/on_the_air")
        return data.get("results", []) or []

    async def get_top_rated_movies(self) -> list[dict]:
        data = await self._get("/movie/top_rated")
        return data.get("results", []) or []

    async def get_top_rated_shows(self) -> list[dict]:
        data = await self._get("/tv/top_rated")
        return data.get("results", []) or []

    async def get_popular_movies(self) -> list[dict]:
        data = await self._get("/movie/popular")
        return data.get("results", []) or []

    async def get_popular_shows(self) -> list[dict]:
        data = await self._get("/tv/popular")
        return data.get("results", []) or []

    async def get_genre_list_movies(self) -> list[dict]:
        data = await self._get("/genre/movie/list", ttl=86400)
        return data.get("genres", []) or []

    async def get_genre_list_shows(self) -> list[dict]:
        data = await self._get("/genre/tv/list", ttl=86400)
        return data.get("genres", []) or []

    async def find_by_imdb_id(self, imdb_id: str) -> dict:
        return await self._get(f"/find/{imdb_id}", params={"external_source": "imdb_id"})

    async def get_person(self, tmdb_person_id: int) -> dict:
        """Person details (used for actor headshots on the dashboard)."""
        return await self._get(f"/person/{tmdb_person_id}")


def _parse_params(parameters: str) -> dict[str, str]:
    """Convert a '&' joined query string into a dict."""
    if not parameters:
        return {}
    out: dict[str, str] = {}
    for part in parameters.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k and v:
                out[k] = v
    return out


_tmdb_client: TMDBClient | None = None


def get_tmdb() -> TMDBClient:
    global _tmdb_client
    if _tmdb_client is None:
        _tmdb_client = TMDBClient()
    return _tmdb_client
