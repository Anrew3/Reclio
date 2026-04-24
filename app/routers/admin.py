"""Admin endpoints: manual sync triggers and operational status.

All endpoints require the `X-Admin-Token` header to match
`settings.admin_token`. If the token is unset, every admin request is
refused — never allow unauthenticated admin access.
"""

from __future__ import annotations

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
from app.models.content import ContentCatalog
from app.models.user import User
from app.services.recombee import get_recombee

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
        user_count = await session.scalar(select(func.count()).select_from(User))
        connected_count = await session.scalar(
            select(func.count()).select_from(User).where(
                User.trakt_access_token_enc.is_not(None)
            )
        )
        profile_ready_count = await session.scalar(
            select(func.count()).select_from(User).where(User.profile_ready.is_(True))
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
        "users": {
            "total": user_count or 0,
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
