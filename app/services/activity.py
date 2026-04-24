"""Per-member /feeds activity tracking. Feeds into the adaptive sync
cadence: users whose Chillio is hitting the endpoint frequently get
synced more often; dormant users less.

Design notes
------------
* We don't want a separate activity-log table for this — too much
  write volume for a tiny signal. A rolling JSON list of timestamps
  on the User row is sufficient and cheap.
* The list is capped (`_MAX_HITS`) so it never grows unbounded even
  for a user hammering the endpoint.
* The pings are fire-and-forget from /feeds: a failure to record
  must never fail the request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User

logger = logging.getLogger(__name__)

# How many timestamps to keep per user. A week with 3/day ≈ 21, so 50
# comfortably covers a full 7-day window for all but the most active.
_MAX_HITS = 50
_WINDOW_DAYS = 7


async def record_feed_hit(
    session: AsyncSession, user: User, *, commit: bool = True
) -> None:
    """Append 'now' to user.recent_feed_hits and update last_feed_request_at.

    Trimmed to the most recent _MAX_HITS entries. Safe to call in a hot
    path: no exception will be raised out of this function.
    """
    try:
        now = datetime.utcnow()
        hits = list(user.recent_feed_hits or [])
        hits.append(now.isoformat())
        if len(hits) > _MAX_HITS:
            hits = hits[-_MAX_HITS:]
        user.recent_feed_hits = hits
        user.last_feed_request_at = now
        if commit:
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_feed_hit failed (ignored): %s", exc)


def hits_in_last_7d(user: User, *, now: datetime | None = None) -> int:
    """Count how many /feeds requests we've seen in the last 7 days."""
    hits = user.recent_feed_hits or []
    if not hits:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=_WINDOW_DAYS)
    count = 0
    for raw in hits:
        try:
            ts = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            count += 1
    return count


def adaptive_sync_interval_hours(user: User) -> int:
    """Pick the cadence (in hours) this user should be synced at.

    Hot  : user_sync_hot_interval_hours      (heavy users)
    Cold : user_sync_cold_interval_hours     (dormant users)
    else : user_sync_default_interval_hours  (the middle band)
    """
    settings = get_settings()
    hits = hits_in_last_7d(user)
    if hits >= settings.user_sync_hot_threshold_per_week:
        return settings.user_sync_hot_interval_hours
    if hits <= settings.user_sync_cold_threshold_per_week:
        return settings.user_sync_cold_interval_hours
    return settings.user_sync_default_interval_hours
