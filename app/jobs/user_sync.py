"""Per-user sync: build taste profile, push interactions to Recombee,
write recommendations back to managed Trakt lists.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import session_scope
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.services.activity import _flatten_activities
from app.services.recombee import get_recombee
from app.services.taste_profile import build_taste_profile
from app.services.trakt import get_trakt
from app.utils.crypto import decrypt

logger = logging.getLogger(__name__)

# Per-user sync locks. Prevents overlapping syncs (e.g. rapid manual refresh
# clicks) from racing on the same user's TasteCache / Recombee state.
_user_sync_locks: dict[str, asyncio.Lock] = {}
_user_sync_locks_guard = asyncio.Lock()


async def _get_user_sync_lock(user_id: str) -> asyncio.Lock:
    async with _user_sync_locks_guard:
        lock = _user_sync_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_sync_locks[user_id] = lock
        return lock


def _normalize_trakt_rating(rating: int | float) -> float:
    """Trakt 1–10 → Recombee rating in [-1.0, 1.0]."""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, (r - 5.5) / 4.5))


def _parse_trakt_ts(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _push_interactions(
    user_id: str,
    access_token: str,
    since: datetime | None,
) -> datetime:
    """Batch-push Trakt history/ratings/watchlist to Recombee. Returns new cutoff."""
    recombee = get_recombee()
    if not recombee.available:
        return datetime.utcnow()

    await recombee.add_user(user_id)
    trakt = get_trakt()

    # gather with return_exceptions=True never raises — it inlines exceptions.
    fetched = await asyncio.gather(
        trakt.get_watch_history(access_token, limit=500, media_type="movies"),
        trakt.get_watch_history(access_token, limit=500, media_type="shows"),
        trakt.get_ratings(access_token, "movies"),
        trakt.get_ratings(access_token, "shows"),
        trakt.get_watchlist(access_token),
        return_exceptions=True,
    )
    movie_history, show_history, movie_ratings, show_ratings, watchlist = fetched

    def _safe(result: Any) -> list:
        return result if isinstance(result, list) else []

    movie_history = _safe(movie_history)
    show_history = _safe(show_history)
    movie_ratings = _safe(movie_ratings)
    show_ratings = _safe(show_ratings)
    watchlist = _safe(watchlist)

    # If every Trakt fetch failed, don't advance the cutoff — Trakt history
    # has no modified_at, so a failed window would silently lose any items
    # added during the outage. Returning `since` keeps the next sync looking
    # back to where we last succeeded.
    all_failed = all(not isinstance(r, list) for r in fetched)
    if all_failed:
        logger.warning(
            "user_sync: all Trakt fetches failed for %s; keeping cutoff", user_id,
        )
        return since or datetime.utcnow()

    cutoff_ts = since.timestamp() if since else 0
    interactions: list[dict[str, Any]] = []

    # --- Watch history → detail views (delta only) ---
    for items, kind in ((movie_history, "movie"), (show_history, "show")):
        item_key = "movie" if kind == "movie" else "show"
        id_prefix = "movie" if kind == "movie" else "tv"
        for entry in items:
            media_obj = entry.get(item_key) or {}
            tmdb_id = (media_obj.get("ids") or {}).get("tmdb")
            if not tmdb_id:
                continue
            ts = _parse_trakt_ts(entry.get("watched_at"))
            if ts and cutoff_ts and ts.timestamp() < cutoff_ts:
                continue
            interactions.append({
                "kind": "view",
                "user_id": user_id,
                "item_id": f"{id_prefix}_{tmdb_id}",
                "timestamp": ts,
            })

    # --- Ratings (full push — ratings are sparse and Recombee dedupes) ---
    for items, kind in ((movie_ratings, "movie"), (show_ratings, "show")):
        item_key = "movie" if kind == "movie" else "show"
        id_prefix = "movie" if kind == "movie" else "tv"
        for entry in items:
            media_obj = entry.get(item_key) or {}
            tmdb_id = (media_obj.get("ids") or {}).get("tmdb")
            rating = entry.get("rating")
            if not tmdb_id or rating is None:
                continue
            ts = _parse_trakt_ts(entry.get("rated_at"))
            interactions.append({
                "kind": "rating",
                "user_id": user_id,
                "item_id": f"{id_prefix}_{tmdb_id}",
                "rating": _normalize_trakt_rating(rating),
                "timestamp": ts,
            })

    # --- Watchlist → bookmarks ---
    for entry in watchlist:
        movie = entry.get("movie") or {}
        show = entry.get("show") or {}
        tmdb_id = None
        prefix = None
        if movie:
            tmdb_id = (movie.get("ids") or {}).get("tmdb")
            prefix = "movie"
        elif show:
            tmdb_id = (show.get("ids") or {}).get("tmdb")
            prefix = "tv"
        if not tmdb_id or not prefix:
            continue
        interactions.append({
            "kind": "bookmark",
            "user_id": user_id,
            "item_id": f"{prefix}_{tmdb_id}",
            "timestamp": _parse_trakt_ts(entry.get("listed_at")),
        })

    if interactions:
        logger.info(
            "user_sync: pushing %d interactions (%d views, %d ratings, %d bookmarks) for %s",
            len(interactions),
            sum(1 for i in interactions if i["kind"] == "view"),
            sum(1 for i in interactions if i["kind"] == "rating"),
            sum(1 for i in interactions if i["kind"] == "bookmark"),
            user_id,
        )
        await recombee.push_interactions_batch(interactions)

    return datetime.utcnow()


async def _refresh_managed_list(
    user: User,
    access_token: str,
    list_id: int | None,
    recommendations: list[str],
    media_type: str,
) -> None:
    """Clear and repopulate a managed Trakt list with Recombee recommendations."""
    if not list_id or not recommendations:
        return
    trakt = get_trakt()
    try:
        await trakt.clear_list(access_token, list_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Clear list %s failed: %s", list_id, exc)
        return

    movies, shows = [], []
    for item_id in recommendations:
        try:
            prefix, tmdb_id = item_id.split("_", 1)
            tmdb_id_int = int(tmdb_id)
        except ValueError:
            continue
        if prefix == "movie" and media_type == "movies":
            movies.append({"ids": {"tmdb": tmdb_id_int}})
        elif prefix == "tv" and media_type == "shows":
            shows.append({"ids": {"tmdb": tmdb_id_int}})

    try:
        await trakt.add_to_list(access_token, list_id, movies=movies or None, shows=shows or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("add_to_list %s failed: %s", list_id, exc)


async def sync_one_user(user_id: str, *, force: bool = False) -> None:
    """Full sync for a single user. Concurrent calls for the same user_id serialize.

    v1.5: starts with a cheap GET /sync/last_activities probe. If
    nothing relevant has moved since the last successful sync, we
    skip the rest of the work and return early. Pass `force=True`
    to bypass the probe (used by manual refresh + onboarding).
    """
    lock = await _get_user_sync_lock(user_id)
    async with lock:
        logger.info("user_sync: starting for %s", user_id)

        async with session_scope() as session:
            user = await session.get(User, user_id)
            if not user or not user.trakt_access_token_enc:
                logger.debug("user_sync: user %s missing or not connected", user_id)
                return
            token = decrypt(user.trakt_access_token_enc)
            if not token:
                logger.warning("user_sync: cannot decrypt token for %s", user_id)
                return

            # ---- Cheap-poll path: fetch /sync/last_activities first ----
            # If nothing relevant has changed since our last snapshot, we
            # skip taste rebuild + interaction push + recommendations
            # entirely. The watch-state evaluator still runs because its
            # state machine depends on TIME elapsed, not on new activity.
            from app.services.activity import activities_changed
            trakt = get_trakt()
            current_activities: dict = {}
            try:
                current_activities = await trakt.get_last_activities(token)
            except Exception as exc:  # noqa: BLE001
                logger.debug("user_sync: last_activities probe failed for %s: %s", user_id, exc)

            changed_keys = activities_changed(user.last_activities_snapshot, current_activities) \
                if current_activities else set()
            heavy_work_needed = bool(changed_keys) or force

            if not heavy_work_needed:
                logger.info("user_sync: %s — no Trakt activity since last sync, light pass only", user_id)
                # Still run watch-state (time-based decisions don't need new history)
                try:
                    from app.jobs.watch_state import evaluate_watch_state
                    counts = await evaluate_watch_state(session, user, history_recent=[])
                    if counts:
                        logger.info("watch_state: user=%s verdicts=%s", user_id, counts)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("watch_state: failed for %s: %s", user_id, exc)
                user.last_seen = datetime.utcnow()
                if current_activities:
                    user.last_activities_snapshot = _flatten_activities(current_activities)
                    user.last_activities_seen_at = datetime.utcnow()
                await session.commit()
                logger.info("user_sync: finished (no-op fast path) for %s", user_id)
                return

            # ---- Full sync ----
            # 1. Build taste profile (uses its own commits internally)
            try:
                await build_taste_profile(session, user_id, user.trakt_access_token_enc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("user_sync: taste_profile failed for %s: %s", user_id, exc)

            last_sync = user.last_history_sync
            new_cutoff = await _push_interactions(user_id, token, last_sync)

            # 1.5. Watch-state machine — turns /sync/playback deltas into
            #      structured signal (completed / abandoned_sleep /
            #      abandoned_bounce / abandoned_lost_interest / accidental).
            #      Pushes Recombee ratings + marks taste cache stale before
            #      step 2 fetches recommendations, so verdicts land on the
            #      same tick they're decided.
            try:
                from app.jobs.watch_state import evaluate_watch_state
                # Pull a recent slice of history for the show-level checks
                # (S1E1 follow-up, lost-interest seasons-watched count).
                hist_pages = await asyncio.gather(
                    trakt.get_watch_history(token, limit=200, media_type="movies"),
                    trakt.get_watch_history(token, limit=200, media_type="shows"),
                    return_exceptions=True,
                )
                history_recent: list[dict] = []
                for p in hist_pages:
                    if isinstance(p, list):
                        history_recent.extend(p)
                playback_now = []
                try:
                    playback_now = await trakt.get_watch_progress(token)
                except Exception:  # noqa: BLE001
                    pass
                ws_counts = await evaluate_watch_state(
                    session, user, history_recent=history_recent, playback_now=playback_now,
                )
                if ws_counts:
                    logger.info("watch_state: user=%s verdicts=%s", user_id, ws_counts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("watch_state: failed for %s: %s", user_id, exc)

            # 2. Pull recommendations from Recombee — both the headline
            #    "Recommended For You" and the BYW lists. Item-to-item
            #    recommendations naturally exclude what the target user
            #    has already watched, so BYW rows never repeat.
            recombee = get_recombee()
            movie_recs: list[str] = []
            show_recs: list[str] = []
            byw_movie_recs: list[str] = []
            byw_show_recs: list[str] = []

            taste = await session.get(TasteCache, user_id)
            byw_movie_anchor = (
                f"movie_{taste.last_watched_movie_tmdb_id}"
                if taste and taste.last_watched_movie_tmdb_id else None
            )
            byw_show_anchor = (
                f"tv_{taste.last_watched_show_tmdb_id}"
                if taste and taste.last_watched_show_tmdb_id else None
            )

            if recombee.available:
                try:
                    coros = [
                        recombee.get_recommendations(user_id, count=50, filter_media_type="movie"),
                        recombee.get_recommendations(user_id, count=50, filter_media_type="tv"),
                        (
                            recombee.recommend_items_to_item(
                                byw_movie_anchor, user_id, count=25, filter_media_type="movie",
                            )
                            if byw_movie_anchor else asyncio.sleep(0, result=[])
                        ),
                        (
                            recombee.recommend_items_to_item(
                                byw_show_anchor, user_id, count=25, filter_media_type="tv",
                            )
                            if byw_show_anchor else asyncio.sleep(0, result=[])
                        ),
                    ]
                    movie_recs, show_recs, byw_movie_recs, byw_show_recs = await asyncio.gather(*coros)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("user_sync: recombee recs failed for %s: %s", user_id, exc)

            # 2.5. Vector-similarity layer — blend semantic neighbors into
            #      BYW lists and seed cold-start "Recommended" rows when
            #      Recombee returned nothing useful (< 12 history items).
            try:
                from app.services.similarity import similar_to
                # Build a watched set so we never recommend something they
                # already saw via the vector path. (Recombee already does
                # this server-side for its own results.)
                watched: set[str] = set()
                # Cheap reuse: anchor history that's already cached.
                for h in (history_recent or []):
                    if h.get("type") == "movie":
                        m = h.get("movie") or {}
                        tmdb = (m.get("ids") or {}).get("tmdb")
                        if tmdb:
                            watched.add(f"movie_{tmdb}")
                    elif h.get("type") == "episode":
                        s = h.get("show") or {}
                        tmdb = (s.get("ids") or {}).get("tmdb")
                        if tmdb:
                            watched.add(f"tv_{tmdb}")

                async def _blend(recs: list[str], anchor: str | None, mt: str) -> list[str]:
                    if not anchor:
                        return recs
                    sim = await similar_to(anchor, k=25, exclude=watched, media_type=mt)
                    if not sim:
                        return recs
                    if not recs:
                        # Cold-start path: pure vector seed.
                        return sim
                    # Blended rank: Recombee items keep their order, then
                    # vector items fill the tail with dedupe.
                    seen = set(recs)
                    blended = list(recs)
                    for s in sim:
                        if s not in seen:
                            blended.append(s)
                            seen.add(s)
                    return blended[:50]

                byw_movie_recs = await _blend(byw_movie_recs, byw_movie_anchor, "movie")
                byw_show_recs = await _blend(byw_show_recs, byw_show_anchor, "tv")

                # Cold-start "Recommended For You": if Recombee gave us
                # very little, seed from the user's last-watched anchor
                # via vector similarity. Skip when Recombee had real recs.
                if len(movie_recs) < 5 and byw_movie_anchor:
                    seed = await similar_to(byw_movie_anchor, k=30, exclude=watched, media_type="movie")
                    if seed:
                        movie_recs = (movie_recs or []) + [s for s in seed if s not in movie_recs]
                        movie_recs = movie_recs[:50]
                if len(show_recs) < 5 and byw_show_anchor:
                    seed = await similar_to(byw_show_anchor, k=30, exclude=watched, media_type="tv")
                    if seed:
                        show_recs = (show_recs or []) + [s for s in seed if s not in show_recs]
                        show_recs = show_recs[:50]
            except Exception as exc:  # noqa: BLE001
                logger.debug("user_sync: similarity blend skipped for %s: %s", user_id, exc)

            # 3. Push recs to Trakt managed lists
            try:
                await asyncio.gather(
                    _refresh_managed_list(user, token, user.trakt_rec_movies_list_id, movie_recs, "movies"),
                    _refresh_managed_list(user, token, user.trakt_rec_shows_list_id, show_recs, "shows"),
                    _refresh_managed_list(user, token, user.trakt_byw_movies_list_id, byw_movie_recs, "movies"),
                    _refresh_managed_list(user, token, user.trakt_byw_shows_list_id, byw_show_recs, "shows"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("user_sync: list refresh failed for %s: %s", user_id, exc)

            user.profile_ready = True
            user.last_history_sync = new_cutoff
            user.last_seen = datetime.utcnow()
            # Persist the activity snapshot so the next tick can short-circuit.
            if current_activities:
                user.last_activities_snapshot = _flatten_activities(current_activities)
                user.last_activities_seen_at = datetime.utcnow()
            await session.commit()

        logger.info("user_sync: finished for %s", user_id)


async def run_user_sync() -> dict[str, int]:
    """Sweep all users that need syncing.

    Each user gets an *adaptive* cadence: heavy users of /feeds get the
    hot interval, dormant users get the cold interval, everyone else the
    default. The sweep itself runs every `user_sync_sweep_interval_hours`
    and picks which users actually need work this tick.
    """
    from app.config import get_settings
    from app.services.activity import adaptive_sync_interval_hours

    settings = get_settings()
    stats = {
        "total": 0, "succeeded": 0, "failed": 0,
        "hot": 0, "default": 0, "cold": 0,
    }
    now = datetime.utcnow()

    async with session_scope() as session:
        q = select(User).where(User.trakt_access_token_enc.is_not(None))
        result = await session.execute(q)
        users = result.scalars().all()

        stale_user_ids: list[str] = []
        for user in users:
            cache = await session.get(TasteCache, user.id)
            interval_hours = adaptive_sync_interval_hours(user)

            # Book-keeping so operators can see the distribution in logs.
            if interval_hours == settings.user_sync_hot_interval_hours:
                stats["hot"] += 1
            elif interval_hours == settings.user_sync_cold_interval_hours:
                stats["cold"] += 1
            else:
                stats["default"] += 1

            needs_sync = (
                not user.profile_ready
                or cache is None
                or cache.is_stale
                or (cache.computed_at is None)
                or (cache.computed_at < now - timedelta(hours=interval_hours))
            )
            if needs_sync:
                stale_user_ids.append(user.id)

    stats["total"] = len(stale_user_ids)

    # Low concurrency to respect third-party limits
    sem = asyncio.Semaphore(2)

    async def _one(uid: str) -> None:
        async with sem:
            try:
                await sync_one_user(uid)
                stats["succeeded"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("user_sync: %s failed: %s", uid, exc)
                stats["failed"] += 1

    await asyncio.gather(*[_one(uid) for uid in stale_user_ids])
    logger.info("user_sync: sweep stats=%s", stats)
    return stats
