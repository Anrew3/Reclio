"""Refresh Trakt OAuth tokens that are within 48h of expiring."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import session_scope
from app.models.user import User
from app.services.trakt import get_trakt
from app.utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)


async def run_token_refresh() -> dict[str, int]:
    stats = {"checked": 0, "refreshed": 0, "failed": 0}
    threshold = datetime.utcnow() + timedelta(hours=48)
    trakt = get_trakt()

    async with session_scope() as session:
        q = select(User).where(
            User.trakt_refresh_token_enc.is_not(None),
            User.trakt_token_expiry.is_not(None),
            User.trakt_token_expiry < threshold,
        )
        result = await session.execute(q)
        users = result.scalars().all()
        stats["checked"] = len(users)

        for user in users:
            refresh_token = decrypt(user.trakt_refresh_token_enc)
            if not refresh_token:
                logger.warning("token_refresh: cannot decrypt refresh token for %s", user.id)
                stats["failed"] += 1
                continue
            try:
                new_tokens = await trakt.refresh_token(refresh_token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("token_refresh: refresh failed for %s: %s", user.id, exc)
                stats["failed"] += 1
                continue

            access = new_tokens.get("access_token")
            refresh = new_tokens.get("refresh_token")
            expires_in = new_tokens.get("expires_in", 7776000)
            if not access or not refresh:
                stats["failed"] += 1
                continue

            user.trakt_access_token_enc = encrypt(access)
            user.trakt_refresh_token_enc = encrypt(refresh)
            user.trakt_token_expiry = datetime.utcnow() + timedelta(seconds=int(expires_in))
            stats["refreshed"] += 1

        await session.commit()

    logger.info("token_refresh: stats=%s", stats)
    return stats
