"""Watch-state machine — turns /sync/playback into structured signal.

Runs inside sync_one_user(), between the interactions push and the
Recombee fetch. Reuses the same per-user lock + adaptive cadence — no
new scheduled job, no new cron entry.

The state machine in `evaluate_watch_state` walks every open
WatchAttempt for the user and applies the rules from V1.5_PLAN.txt
section 2:

  Movies:
    -  >=90% progress     → completed
    -  appears in history → completed
    -  <5% + 24h+ idle    → accidental (no signal)
    -  late-night drop + 5d+ idle  → abandoned_sleep (mild negative)
    -  daytime drop + 24h+ idle    → abandoned_bounce (strong negative)
    -  otherwise stays in_progress

  Shows:
    -  S1E1 + <50% progress + 48h+ idle  → abandoned_bounce (loudest)
    -  S1E1 completed but no E2 in 7d    → abandoned_bounce (mild)
    -  2+ seasons watched + 14d+ idle    → abandoned_lost_interest (positive)
    -  mid-season pause                  → stays in_progress

When a verdict flips, we push the corresponding signal to Recombee +
optionally bump the taste cache, then set feedback_pushed=True so
re-running the evaluator never double-counts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.taste_cache import TasteCache
from app.models.user import User
from app.models.watch_attempt import WatchAttempt
from app.services.recombee import get_recombee
from app.services.trakt import get_trakt

logger = logging.getLogger(__name__)

# Sleep window — late-night local hours where a drop is most likely
# "fell asleep" rather than "didn't like it". Kept wide so DST jumps
# never push a non-sleep event into the window or vice versa.
_SLEEP_HOURS: set[int] = {22, 23, 0, 1, 2, 3, 4}

# Verdict timeouts (the gap after which a stalled attempt is decided).
_T_ACCIDENTAL_HOURS = 24       # <5% + this old → accidental
_T_BOUNCE_HOURS = 24           # daytime drop + this old → abandoned_bounce
_T_SLEEP_DAYS = 5              # late-night drop + this old → abandoned_sleep
_T_S1E1_BOUNCE_HOURS = 48      # first-ep drop + this old → abandoned_bounce
_T_E2_FOLLOWUP_DAYS = 7        # finished S1E1 with no E2 in this many days → bounce
_T_LOST_INTEREST_DAYS = 14     # 2+ seasons watched + this idle → lost_interest


def _local_hour(paused_at_utc: datetime | None, tz_name: str) -> int | None:
    """Convert a UTC instant to the user's local hour-of-day [0, 23]."""
    if paused_at_utc is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    if paused_at_utc.tzinfo is None:
        paused_at_utc = paused_at_utc.replace(tzinfo=timezone.utc)
    return paused_at_utc.astimezone(tz).hour


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _attempt_for_playback_entry(
    entry: dict, user_tz: str
) -> tuple[int, str, dict[str, Any]] | None:
    """Pull the fields we care about from a Trakt playback entry.

    Returns (trakt_playback_id, kind, attrs_dict) or None if the entry
    is malformed or missing the bits we need to identify the item.
    """
    pid = entry.get("id")
    if not pid:
        return None
    progress = float(entry.get("progress") or 0.0)
    paused_at_utc = _parse_iso(entry.get("paused_at"))
    local_hour = _local_hour(paused_at_utc, user_tz)

    entry_type = entry.get("type")
    if entry_type == "movie":
        movie = entry.get("movie") or {}
        tmdb = (movie.get("ids") or {}).get("tmdb")
        if not tmdb:
            return None
        return pid, "movie", {
            "movie_tmdb_id": int(tmdb),
            "show_tmdb_id": None,
            "episode_number": None,
            "season_number": None,
            "last_progress_pct": progress,
            "last_paused_at_utc": paused_at_utc.replace(tzinfo=None) if paused_at_utc else None,
            "last_paused_local_hour": local_hour,
        }
    if entry_type == "episode":
        episode = entry.get("episode") or {}
        show = entry.get("show") or {}
        tmdb = (show.get("ids") or {}).get("tmdb")
        if not tmdb:
            return None
        return pid, "episode", {
            "movie_tmdb_id": None,
            "show_tmdb_id": int(tmdb),
            "episode_number": episode.get("number"),
            "season_number": episode.get("season"),
            "last_progress_pct": progress,
            "last_paused_at_utc": paused_at_utc.replace(tzinfo=None) if paused_at_utc else None,
            "last_paused_local_hour": local_hour,
        }
    return None


def _movie_in_history(history_recent: list[dict], tmdb_id: int) -> bool:
    """Did this movie tmdb_id appear as a completed watch since last sync?"""
    for h in history_recent:
        if h.get("type") != "movie":
            continue
        m = h.get("movie") or {}
        if int((m.get("ids") or {}).get("tmdb") or 0) == tmdb_id:
            return True
    return False


def _episode_in_history(history_recent: list[dict], show_tmdb_id: int,
                        season: int | None, episode: int | None) -> bool:
    for h in history_recent:
        if h.get("type") != "episode":
            continue
        s = h.get("show") or {}
        e = h.get("episode") or {}
        if int((s.get("ids") or {}).get("tmdb") or 0) != show_tmdb_id:
            continue
        if season is not None and e.get("season") != season:
            continue
        if episode is not None and e.get("number") != episode:
            continue
        return True
    return False


def _e2_seen_within(
    history_recent: list[dict], show_tmdb_id: int, days: int
) -> bool:
    """Did the user watch S1E2 (or later) of this show within `days`?"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    for h in history_recent:
        if h.get("type") != "episode":
            continue
        s = h.get("show") or {}
        e = h.get("episode") or {}
        if int((s.get("ids") or {}).get("tmdb") or 0) != show_tmdb_id:
            continue
        if (e.get("season") or 0) >= 1 and (e.get("number") or 0) >= 2:
            ts = _parse_iso(h.get("watched_at"))
            if ts and ts.replace(tzinfo=None) >= cutoff:
                return True
    return False


def _seasons_watched(history_recent: list[dict], show_tmdb_id: int) -> set[int]:
    """Set of season numbers the user has any history for."""
    out: set[int] = set()
    for h in history_recent:
        if h.get("type") != "episode":
            continue
        s = h.get("show") or {}
        if int((s.get("ids") or {}).get("tmdb") or 0) != show_tmdb_id:
            continue
        season = (h.get("episode") or {}).get("season")
        if season is not None:
            out.add(int(season))
    return out


def _decide_movie(att: WatchAttempt, history_recent: list[dict],
                  now: datetime, in_playback: bool) -> str | None:
    """Return the new status (or None to leave unchanged)."""
    if att.movie_tmdb_id and _movie_in_history(history_recent, att.movie_tmdb_id):
        return "completed"
    if att.last_progress_pct >= 90:
        return "completed"

    age = now - (att.last_paused_at_utc or att.first_seen_at)

    if att.last_progress_pct < 5 and age >= timedelta(hours=_T_ACCIDENTAL_HOURS):
        return "accidental"

    if 5 <= att.last_progress_pct <= 90:
        local_h = att.last_paused_local_hour
        if local_h in _SLEEP_HOURS and age >= timedelta(days=_T_SLEEP_DAYS):
            return "abandoned_sleep"
        if (local_h is None or local_h not in _SLEEP_HOURS) \
                and age >= timedelta(hours=_T_BOUNCE_HOURS) and not in_playback:
            return "abandoned_bounce"

    return None


def _decide_episode(att: WatchAttempt, history_recent: list[dict],
                    now: datetime, in_playback: bool) -> str | None:
    if not att.show_tmdb_id:
        return None

    if _episode_in_history(history_recent, att.show_tmdb_id,
                           att.season_number, att.episode_number):
        # Episode is now in history. Nothing more to decide on this row;
        # any abandonment signal would come from a SEPARATE attempt
        # (the next pause). Mark completed; the show-level "no E2 follow-up"
        # check happens via a fresh evaluation pass below.
        return "completed"

    age = now - (att.last_paused_at_utc or att.first_seen_at)

    is_s1e1 = att.season_number == 1 and att.episode_number == 1

    if is_s1e1 and att.last_progress_pct < 50 \
            and age >= timedelta(hours=_T_S1E1_BOUNCE_HOURS) and not in_playback:
        return "abandoned_bounce"

    seasons = _seasons_watched(history_recent, att.show_tmdb_id)
    if len(seasons) >= 2 and age >= timedelta(days=_T_LOST_INTEREST_DAYS) \
            and not in_playback:
        return "abandoned_lost_interest"

    return None


# Recombee rating amplitudes per verdict. Magnitudes are in the [-1, 1]
# range Recombee expects. None = no Recombee signal for this verdict.
_RECOMBEE_RATING: dict[str, float | None] = {
    "completed":               +0.5,
    "abandoned_sleep":         -0.2,
    "abandoned_bounce":        -0.7,
    # NOTE: a S1E1 bounce on a SHOW gets the strongest signal (-1.0), applied
    # below by special-casing the kind=='episode' path before this lookup.
    "abandoned_lost_interest": None,
    "accidental":              None,
}


async def _push_signal(att: WatchAttempt) -> bool:
    """Push the Recombee signal for the verdict. Returns True on success."""
    recombee = get_recombee()
    if not recombee.available:
        return True  # Recombee disabled — treat as "pushed" so we don't loop

    rating = _RECOMBEE_RATING.get(att.status)
    # Show-level S1E1 bounce gets the loudest signal.
    if att.status == "abandoned_bounce" and att.kind == "episode" \
            and att.season_number == 1 and att.episode_number == 1:
        rating = -1.0
    if rating is None:
        return True

    # Build the canonical Recombee item id. Show-level signals (lost_interest,
    # S1E1 bounce, episode completion) attach to the SHOW. Movie signals attach
    # to the movie.
    if att.kind == "movie" and att.movie_tmdb_id:
        item_id = f"movie_{att.movie_tmdb_id}"
    elif att.kind == "episode" and att.show_tmdb_id:
        item_id = f"tv_{att.show_tmdb_id}"
    else:
        return True

    try:
        rq = recombee._rq  # noqa: SLF001
        import asyncio
        await asyncio.to_thread(
            recombee._client.send,  # noqa: SLF001
            rq.AddRating(att.user_id, item_id, rating, cascade_create=True),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("watch_state: Recombee push failed for %s/%s: %s",
                     att.user_id, item_id, exc)
        return False


async def evaluate_watch_state(
    session: AsyncSession,
    user: User,
    history_recent: list[dict],
    playback_now: list[dict] | None = None,
) -> dict[str, int]:
    """Walk all open WatchAttempts + reconcile against current playback.

    Steps:
      1. Pull /sync/playback if not provided. Build {playback_id: entry}.
      2. Upsert one WatchAttempt per playback entry (create or update).
      3. For each open WatchAttempt, decide a verdict. Push signals.

    Returns {verdict_name: count} for logging.
    """
    if playback_now is None:
        from app.utils.crypto import decrypt
        token = decrypt(user.trakt_access_token_enc) if user.trakt_access_token_enc else None
        if not token:
            return {}
        try:
            trakt = get_trakt()
            playback_now = await trakt.get_watch_progress(token)
        except Exception as exc:  # noqa: BLE001
            logger.debug("watch_state: playback fetch failed for %s: %s", user.id, exc)
            return {}

    user_tz = user.timezone or "UTC"
    now = datetime.utcnow()
    counts: dict[str, int] = {}

    # ---- Step 1: upsert from playback ------------------------------
    by_id: dict[int, dict[str, Any]] = {}
    for entry in playback_now or []:
        parsed = _attempt_for_playback_entry(entry, user_tz)
        if parsed is None:
            continue
        pid, kind, attrs = parsed
        by_id[pid] = {"kind": kind, **attrs}

    # Fetch existing attempts for this user (open + recently decided).
    result = await session.execute(
        select(WatchAttempt).where(WatchAttempt.user_id == user.id)
    )
    existing = {a.trakt_playback_id: a for a in result.scalars().all()}

    for pid, attrs in by_id.items():
        att = existing.get(pid)
        if att is None:
            att = WatchAttempt(
                user_id=user.id,
                trakt_playback_id=pid,
                kind=attrs["kind"],
                first_seen_at=now,
            )
            session.add(att)
            existing[pid] = att
        # Always refresh the live fields. New paused_at = new clock.
        att.kind = attrs["kind"]
        att.movie_tmdb_id = attrs["movie_tmdb_id"]
        att.show_tmdb_id = attrs["show_tmdb_id"]
        att.episode_number = attrs["episode_number"]
        att.season_number = attrs["season_number"]
        att.last_progress_pct = attrs["last_progress_pct"]
        att.last_paused_at_utc = attrs["last_paused_at_utc"]
        att.last_paused_local_hour = attrs["last_paused_local_hour"]

    # ---- Step 2: decide each in_progress attempt -------------------
    for att in existing.values():
        if att.status != "in_progress":
            continue
        in_playback = att.trakt_playback_id in by_id
        verdict = (
            _decide_movie(att, history_recent, now, in_playback)
            if att.kind == "movie"
            else _decide_episode(att, history_recent, now, in_playback)
        )
        if verdict and verdict != att.status:
            att.status = verdict
            att.decided_at = now
            counts[verdict] = counts.get(verdict, 0) + 1

    # ---- Step 3: push signals for newly-decided attempts ----------
    pending = [a for a in existing.values()
               if a.decided_at is not None and not a.feedback_pushed
               and a.status != "in_progress"]
    for att in pending:
        ok = await _push_signal(att)
        if ok:
            att.feedback_pushed = True

    # ---- Step 4: also detect "S1E1 completed but no E2 follow-up
    # within 7 days" — this is a different verdict path because the
    # attempt's status is `completed` (positive) yet the show-level
    # signal is mildly negative. We track this by scanning recently
    # completed S1E1 episode attempts and checking E2 follow-up.
    cutoff = now - timedelta(days=_T_E2_FOLLOWUP_DAYS)
    for att in existing.values():
        if att.status != "completed":
            continue
        if att.kind != "episode":
            continue
        if att.season_number != 1 or att.episode_number != 1:
            continue
        if att.feedback_pushed:
            continue
        if att.decided_at is None or att.decided_at > cutoff:
            continue
        if _e2_seen_within(history_recent, att.show_tmdb_id or 0, _T_E2_FOLLOWUP_DAYS):
            att.feedback_pushed = True   # they continued — neutral
            continue
        # Promote to mild bounce; reset the rating to a softer value
        # so the negative signal doesn't equal a true "didn't even
        # finish" S1E1 bounce.
        att.status = "abandoned_bounce"
        recombee = get_recombee()
        if recombee.available and att.show_tmdb_id:
            try:
                import asyncio
                rq = recombee._rq  # noqa: SLF001
                await asyncio.to_thread(
                    recombee._client.send,  # noqa: SLF001
                    rq.AddRating(att.user_id, f"tv_{att.show_tmdb_id}", -0.4,
                                 cascade_create=True),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("watch_state: E2-followup push failed: %s", exc)
        att.feedback_pushed = True
        counts["abandoned_bounce_followup"] = counts.get("abandoned_bounce_followup", 0) + 1

    # If anything decided, mark the taste cache stale so the next sync
    # rebuilds rec lists with the new dislike weight applied.
    if counts:
        cache = await session.get(TasteCache, user.id)
        if cache is not None:
            cache.is_stale = True

    return counts
