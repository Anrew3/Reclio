"""Trakt.tv API client — OAuth, history, ratings, watchlist, managed lists.

Read endpoints are cached with short TTLs to absorb duplicate fetches. The
common pattern in `sync_one_user` is `build_taste_profile()` and
`_push_interactions()` both pulling history + ratings back-to-back — without
the cache, that's 8 redundant Trakt calls per sync. The cache key is keyed
on a hash of the access token so different users never share entries.

Cache TTLs (per endpoint):
  history          5 min  (most dynamic)
  ratings          15 min
  watchlist        15 min
  watch_progress   2 min  (used by Continue Watching row)
  user_profile     1 hour (almost never changes)
  user_lists       30 min
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)


def _token_key(token: str) -> str:
    """Short hash for use in cache keys — never store raw tokens in keys."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class TraktError(Exception):
    pass


class TraktClient:
    BASE_URL = "https://api.trakt.tv"
    OAUTH_URL = "https://trakt.tv/oauth/authorize"
    TOKEN_URL = "https://api.trakt.tv/oauth/token"
    API_VERSION = "2"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        settings = get_settings()
        self.client_id = client_id or settings.trakt_client_id
        self.client_secret = client_secret or settings.trakt_client_secret
        self.redirect_uri = redirect_uri or settings.trakt_redirect_uri
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(5)
        # Per-client cache. Bounded at 4096 entries (default) and LRU-evicted.
        self._cache = TTLCache(default_ttl=300.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=httpx.Timeout(15.0, connect=5.0),
                headers={
                    "Content-Type": "application/json",
                    "trakt-api-version": self.API_VERSION,
                    "trakt-api-key": self.client_id,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    # --- Request helper with rate-limit handling ------------------

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str | None = None,
        params: dict | None = None,
        json: Any | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if access_token:
            headers.update(self._auth_headers(access_token))

        async with self._semaphore:
            client = await self._get_client()
            for attempt in range(4):
                try:
                    resp = await client.request(
                        method, path, params=params, json=json, headers=headers
                    )
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", "2"))
                        await asyncio.sleep(retry_after + 0.5)
                        continue
                    if 500 <= resp.status_code < 600:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    if resp.status_code == 204:
                        return None
                    if resp.status_code >= 400:
                        text = resp.text[:300]
                        logger.warning(
                            "Trakt %s %s returned %s: %s",
                            method, path, resp.status_code, text,
                        )
                        raise TraktError(f"Trakt error {resp.status_code}: {text}")
                    return resp.json() if resp.content else None
                except httpx.HTTPError as exc:
                    if attempt == 3:
                        raise TraktError(f"Trakt network error: {exc}") from exc
                    await asyncio.sleep(0.8 * (attempt + 1))
        raise TraktError("Trakt: exhausted retries")

    # --- OAuth ---------------------------------------------------

    def build_authorize_url(self, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{self.OAUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        payload = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        return await self._request("POST", "/oauth/token", json=payload)

    async def refresh_token(self, refresh_token: str) -> dict:
        payload = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "refresh_token",
        }
        return await self._request("POST", "/oauth/token", json=payload)

    # --- User data ----------------------------------------------
    #
    # All read methods route through the per-client TTL cache. The token
    # contributes to the cache key as a short hash so different users
    # never collide.  Mutating endpoints stay uncached.

    async def get_user_profile(self, token: str) -> dict:
        key = f"profile:{_token_key(token)}"

        async def _fetch() -> dict:
            # extended=full returns the user's timezone field, used by
            # the watch-state machine's sleep heuristic.
            return await self._request(
                "GET", "/users/me", access_token=token, params={"extended": "full"},
            )

        return await self._cache.get_or_set(key, _fetch, ttl=3600)

    async def get_last_activities(self, token: str) -> dict:
        """Cheap "did anything change?" probe.

        Single GET returning one timestamp per category (movies.watched_at,
        episodes.watched_at, movies.paused_at, etc.). Lets us skip the bulk
        of a sync tick when nothing relevant has moved since last time.

        Cache TTL is intentionally short (60 s) so a hot user reflects live
        activity while still de-duping calls within a single sync_one_user
        pass.
        """
        key = f"activities:{_token_key(token)}"

        async def _fetch() -> dict:
            data = await self._request("GET", "/sync/last_activities", access_token=token)
            return data or {}

        return await self._cache.get_or_set(key, _fetch, ttl=60)

    async def get_watch_history(
        self, token: str, limit: int = 100, page: int = 1, media_type: str | None = None
    ) -> list[dict]:
        path = "/sync/history"
        if media_type in {"movies", "shows", "episodes"}:
            path = f"/sync/history/{media_type}"
        key = f"history:{_token_key(token)}:{media_type or 'all'}:{limit}:{page}"

        async def _fetch() -> list[dict]:
            params = {"limit": limit, "page": page, "extended": "min"}
            data = await self._request("GET", path, access_token=token, params=params)
            return data or []

        return await self._cache.get_or_set(key, _fetch, ttl=300)

    async def get_ratings(self, token: str, media_type: str | None = None) -> list[dict]:
        path = "/sync/ratings"
        if media_type in {"movies", "shows", "episodes"}:
            path = f"/sync/ratings/{media_type}"
        key = f"ratings:{_token_key(token)}:{media_type or 'all'}"

        async def _fetch() -> list[dict]:
            data = await self._request("GET", path, access_token=token)
            return data or []

        return await self._cache.get_or_set(key, _fetch, ttl=900)

    async def get_watchlist(self, token: str) -> list[dict]:
        key = f"watchlist:{_token_key(token)}"

        async def _fetch() -> list[dict]:
            data = await self._request("GET", "/sync/watchlist", access_token=token)
            return data or []

        return await self._cache.get_or_set(key, _fetch, ttl=900)

    async def get_watch_progress(self, token: str) -> list[dict]:
        # Trakt's sync/playback endpoint returns in-progress items.
        key = f"progress:{_token_key(token)}"

        async def _fetch() -> list[dict]:
            data = await self._request("GET", "/sync/playback", access_token=token)
            return data or []

        return await self._cache.get_or_set(key, _fetch, ttl=120)

    async def get_user_lists(self, token: str) -> list[dict]:
        key = f"lists:{_token_key(token)}"

        async def _fetch() -> list[dict]:
            data = await self._request("GET", "/users/me/lists", access_token=token)
            return data or []

        return await self._cache.get_or_set(key, _fetch, ttl=1800)

    def invalidate_user_cache(self, token: str) -> None:
        """Drop every cache entry for this token. Call after we mutate the
        user's Trakt state (clear/add to managed lists, create lists)."""
        prefix = _token_key(token)
        for entry_key in list(self._cache._data.keys()):  # noqa: SLF001
            if prefix in entry_key:
                self._cache.invalidate(entry_key)

    # --- Managed lists -------------------------------------------

    async def create_list(
        self, token: str, name: str, description: str = "", privacy: str = "private"
    ) -> dict:
        payload = {
            "name": name,
            "description": description,
            "privacy": privacy,
            "display_numbers": False,
            "allow_comments": False,
        }
        return await self._request(
            "POST", "/users/me/lists", access_token=token, json=payload
        )

    async def clear_list(self, token: str, list_id: int) -> None:
        # Fetch items first
        items = await self._request(
            "GET", f"/users/me/lists/{list_id}/items", access_token=token
        ) or []
        if not items:
            return
        movies, shows = [], []
        for it in items:
            m = it.get("movie")
            s = it.get("show")
            if m and m.get("ids"):
                movies.append({"ids": m["ids"]})
            elif s and s.get("ids"):
                shows.append({"ids": s["ids"]})
        payload: dict[str, Any] = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if not payload:
            return
        await self._request(
            "POST",
            f"/users/me/lists/{list_id}/items/remove",
            access_token=token,
            json=payload,
        )

    async def add_to_list(
        self,
        token: str,
        list_id: int,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if not payload:
            return {}
        return await self._request(
            "POST",
            f"/users/me/lists/{list_id}/items",
            access_token=token,
            json=payload,
        )


_trakt: TraktClient | None = None


def get_trakt() -> TraktClient:
    global _trakt
    if _trakt is None:
        _trakt = TraktClient()
    return _trakt
