"""Admin endpoints: manual sync triggers and operational status.

All endpoints require the `X-Admin-Token` header to match
`settings.admin_token`. If the token is unset, every admin request is
refused — never allow unauthenticated admin access.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Path, status
from sqlalchemy import func, select

from app.config import get_settings
from app.database import session_scope
from app.jobs.content_sync import run_content_sync
from app.jobs.user_sync import sync_one_user
from app.models.account import Account
from app.models.content import ContentCatalog
from app.models.user import User
from app.models.watch_attempt import WatchAttempt
from app.services.recombee import get_recombee
from app.services.similarity import similar_to

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(x_admin_token: str | None) -> None:
    configured = get_settings().admin_token
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin endpoints disabled (ADMIN_TOKEN not configured)",
        )
    # Constant-time comparison to block timing-leak attacks on the token
    if not x_admin_token or not secrets.compare_digest(x_admin_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
        )


def _validate_user_id(user_id: str) -> str:
    try:
        return str(uuid.UUID(user_id))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid user_id format",
        ) from None


async def _run_content_sync_safe() -> None:
    try:
        await run_content_sync()
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin: content sync failed: %s", exc)


async def _run_user_sync_safe(user_id: str) -> None:
    try:
        await sync_one_user(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin: user sync failed for %s: %s", user_id, exc)


@router.post("/sync/content", status_code=status.HTTP_202_ACCEPTED)
async def trigger_content_sync(
    background: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, str]:
    _require_admin(x_admin_token)
    background.add_task(_run_content_sync_safe)
    return {"status": "scheduled", "job": "content_sync"}


@router.post("/sync/user/{user_id}", status_code=status.HTTP_202_ACCEPTED)
async def trigger_user_sync(
    background: BackgroundTasks,
    user_id: str = Path(..., min_length=8, max_length=64),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, str]:
    _require_admin(x_admin_token)
    user_id = _validate_user_id(user_id)
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="user not found",
            )
    background.add_task(_run_user_sync_safe, user_id)
    return {"status": "scheduled", "job": "user_sync", "user_id": user_id}


@router.get("/status")
async def admin_status(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)

    recombee = get_recombee()

    async with session_scope() as session:
        account_count = await session.scalar(select(func.count()).select_from(Account))
        member_count = await session.scalar(select(func.count()).select_from(User))
        connected_count = await session.scalar(
            select(func.count()).select_from(User).where(
                User.trakt_access_token_enc.is_not(None)
            )
        )
        profile_ready_count = await session.scalar(
            select(func.count()).select_from(User).where(User.profile_ready.is_(True))
        )
        # Accounts with more than one member = active family setups
        family_accounts_count = await session.scalar(
            select(func.count()).select_from(
                select(User.account_id)
                .where(User.account_id.is_not(None))
                .group_by(User.account_id)
                .having(func.count() > 1)
                .subquery()
            )
        )
        last_user_sync = await session.scalar(select(func.max(User.last_history_sync)))

        content_count = await session.scalar(
            select(func.count()).select_from(ContentCatalog)
        )
        recombee_synced_count = await session.scalar(
            select(func.count()).select_from(ContentCatalog).where(
                ContentCatalog.recombee_synced.is_(True)
            )
        )
        embedded_count = await session.scalar(
            select(func.count()).select_from(ContentCatalog).where(
                ContentCatalog.embedding_stored.is_(True)
            )
        )
        last_content_update = await session.scalar(
            select(func.max(ContentCatalog.last_updated))
        )

    return {
        "recombee": {"available": recombee.available},
        "accounts": {
            "total": account_count or 0,
            "with_multiple_members": family_accounts_count or 0,
        },
        "members": {
            "total": member_count or 0,
            "connected": connected_count or 0,
            "profile_ready": profile_ready_count or 0,
            "last_history_sync": last_user_sync.isoformat() if last_user_sync else None,
        },
        "content": {
            "total": content_count or 0,
            "recombee_synced": recombee_synced_count or 0,
            "embedding_stored": embedded_count or 0,
            "last_updated": last_content_update.isoformat() if last_content_update else None,
        },
    }


@router.get("/recombee/preview/{user_id}")
async def recombee_preview(
    user_id: str = Path(..., min_length=8, max_length=64),
    count: int = 10,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Return Recombee's raw recommendations for a user, split by media type.

    Useful for sanity-checking what the rec engine is producing without
    waiting for the next user_sync sweep to materialize a Trakt list.

    Item IDs are Reclio's internal format (`movie_<tmdb_id>` / `tv_<tmdb_id>`).
    Empty lists usually mean the user hasn't been synced yet, or Recombee
    hasn't seen enough interactions to recommend anything.
    """
    _require_admin(x_admin_token)
    user_id = _validate_user_id(user_id)
    count = max(1, min(50, count))

    recombee = get_recombee()
    if not recombee.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="recombee unavailable (check RECOMBEE_DATABASE_ID / RECOMBEE_PRIVATE_TOKEN)",
        )

    movies, shows = await asyncio.gather(
        recombee.get_recommendations(user_id, count=count, filter_media_type="movie"),
        recombee.get_recommendations(user_id, count=count, filter_media_type="tv"),
    )
    return {
        "user_id": user_id,
        "count": count,
        "movies": movies,
        "shows": shows,
        "totals": {"movies": len(movies), "shows": len(shows)},
    }


@router.get("/watch_attempts/{user_id}")
async def watch_attempts(
    user_id: str = Path(..., min_length=8, max_length=64),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """v1.5 watch-state inspection.

    Returns every WatchAttempt for the user grouped by status, with
    counts and the latest attempts inline. Useful for debugging the
    sleep / bounce / lost_interest verdict logic.
    """
    _require_admin(x_admin_token)
    user_id = _validate_user_id(user_id)

    async with session_scope() as session:
        result = await session.execute(
            select(WatchAttempt).where(WatchAttempt.user_id == user_id)
        )
        rows = result.scalars().all()

    by_status: dict[str, list[dict]] = {}
    for r in rows:
        entry = {
            "id": r.id,
            "kind": r.kind,
            "movie_tmdb_id": r.movie_tmdb_id,
            "show_tmdb_id": r.show_tmdb_id,
            "season_number": r.season_number,
            "episode_number": r.episode_number,
            "last_progress_pct": round(r.last_progress_pct or 0.0, 1),
            "last_paused_at_utc": r.last_paused_at_utc.isoformat() if r.last_paused_at_utc else None,
            "last_paused_local_hour": r.last_paused_local_hour,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "feedback_pushed": r.feedback_pushed,
            "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
            "trakt_playback_id": r.trakt_playback_id,
        }
        by_status.setdefault(r.status, []).append(entry)

    counts = {status: len(entries) for status, entries in by_status.items()}
    return {
        "user_id": user_id,
        "total": len(rows),
        "counts": counts,
        "by_status": by_status,
    }


@router.get("/similar/{seed_id}")
async def admin_similar(
    seed_id: str = Path(..., min_length=4, max_length=40),
    k: int = 12,
    media_type: str | None = None,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """v1.5 vector-similarity sanity check.

    seed_id format: "movie_<tmdb>" or "tv_<tmdb>" (canonical Reclio id).
    Returns the top-k semantically similar items from the embedding
    catalog. Empty when embeddings haven't been computed yet (run
    /admin/sync/content first).
    """
    _require_admin(x_admin_token)
    if not (seed_id.startswith("movie_") or seed_id.startswith("tv_")):
        raise HTTPException(status_code=400, detail="seed_id must be 'movie_<tmdb>' or 'tv_<tmdb>'")
    k = max(1, min(50, k))
    if media_type and media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

    results = await similar_to(seed_id, k=k, media_type=media_type)

    # Hydrate with titles from the catalog so the response is readable.
    titled: list[dict] = []
    if results:
        async with session_scope() as session:
            res = await session.execute(
                select(ContentCatalog.tmdb_id, ContentCatalog.title, ContentCatalog.year)
                .where(ContentCatalog.tmdb_id.in_(results))
            )
            title_map = {tid: (title, year) for tid, title, year in res.all()}
        for rid in results:
            t, y = title_map.get(rid, (None, None))
            titled.append({"id": rid, "title": t, "year": y})

    return {"seed_id": seed_id, "k": k, "media_type": media_type, "results": titled}
