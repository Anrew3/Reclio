"""Local-first recommendation engine.

This module is the single seam every caller goes through for
recommendation reads and interaction writes. The backend is picked by
`RECOMMENDER` (settings.recommender):

    local     (default) — fully self-contained. Interactions live in the
                `interactions` SQLite table; recommendations come from a
                recency-weighted taste-profile vector scored against the
                catalog embedding matrix (see similarity.py), with a
                popularity prior and a pure-popularity fallback so rows
                are never empty. No third-party recommendation API.
    recombee  (legacy) — proxies to the Recombee SaaS as pre-1.7
                versions did. Requires the recombee-api-client package
                and RECOMBEE_* env vars.

Interactions are ALWAYS recorded locally, even in recombee mode, so an
instance can switch to `local` later without losing its history.

Profile math (local mode):
    profile = Σ  kind_weight × recency_decay(happened_at) × embedding(item)
    score   = cosine(profile, item) + 0.15 × popularity_norm(item)

Negative signal (blocks, bounces, low ratings) subtracts from the
profile, steering the ranking away from similar items — the same
effect Recombee's collaborative model approximated, but transparent
and local.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.database import session_scope
from app.models.content import ContentCatalog
from app.models.interaction import Interaction
from app.models.preferences import UserPreferences
from app.services import similarity

logger = logging.getLogger(__name__)

# Contribution multiplier per interaction kind. `rating`, `signal` and
# `block` rows carry their own signed weight in [-1, 1]; these factors
# set how loudly each kind speaks relative to a plain view.
_KIND_FACTOR: dict[str, float] = {
    "view": 1.0,
    "rating": 1.6,     # explicit opinion beats implicit watch
    "bookmark": 0.7,   # intent, not experience
    "signal": 1.2,     # watch-state verdicts (completed / abandoned_*)
    "block": 1.8,      # "never again" — loudest negative
}

# Recency half-life for the profile vector. Shorter than the taste
# profile's 2 years: the rec row should chase current mood.
_PROFILE_HALF_LIFE_DAYS = 270.0

# Popularity prior weight in the blended score (see similarity.rank_by_vector).
_POPULARITY_WEIGHT = 0.15

# Quality floor for the popularity fallback so cold-start rows aren't
# filled with high-popularity shovelware.
_FALLBACK_MIN_VOTE = 6.0


def _backend() -> str:
    return get_settings().recommender


def _recency_decay(happened_at: datetime | None) -> float:
    if happened_at is None:
        return 1.0
    age_days = max(0.0, (datetime.utcnow() - happened_at).total_seconds() / 86400.0)
    return math.pow(2.0, -age_days / _PROFILE_HALF_LIFE_DAYS)


# ============================================================
# Interaction writes — always local, mirrored to Recombee when
# that backend is active.
# ============================================================


async def record_interactions(interactions: list[dict[str, Any]]) -> int:
    """Upsert a batch of interactions into the local store.

    Each dict: {"kind", "user_id", "item_id", "weight"?, "timestamp"?}
    (the same shape user_sync builds for the old Recombee push, plus
    "rating" is accepted as an alias for "weight" on rating rows).
    Returns the number of rows written.
    """
    rows: list[dict[str, Any]] = []
    now = datetime.utcnow()
    for it in interactions:
        kind = it.get("kind")
        user_id = it.get("user_id")
        item_id = it.get("item_id")
        if not (kind and user_id and item_id):
            continue
        weight = it.get("weight")
        if weight is None:
            weight = it.get("rating")
        if weight is None:
            weight = {"view": 1.0, "bookmark": 0.7}.get(kind, 1.0)
        ts = it.get("timestamp")
        if isinstance(ts, datetime):
            ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
        else:
            ts = None
        rows.append({
            "user_id": user_id,
            "item_id": item_id,
            "kind": kind,
            "weight": max(-1.0, min(1.0, float(weight))),
            "happened_at": ts,
            "updated_at": now,
        })

    if not rows:
        return 0

    # Dedupe within the batch (a rewatched movie appears in history many
    # times) — keep the most recent event per (user, item, kind).
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["user_id"], r["item_id"], r["kind"])
        prev = by_key.get(key)
        if prev is None or (r["happened_at"] or datetime.min) >= (prev["happened_at"] or datetime.min):
            by_key[key] = r
    rows = list(by_key.values())

    async with session_scope() as session:
        stmt = sqlite_insert(Interaction).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "item_id", "kind"],
            set_={
                "weight": stmt.excluded.weight,
                "happened_at": stmt.excluded.happened_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)
    return len(rows)


async def push_interactions(interactions: list[dict[str, Any]]) -> dict[str, int]:
    """Record interactions locally; mirror to Recombee in recombee mode."""
    stored = await record_interactions(interactions)
    stats = {"stored_local": stored}
    if _backend() == "recombee":
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        if recombee.available:
            stats.update(await recombee.push_interactions_batch(interactions))
    return stats


async def push_signal(user_id: str, item_id: str, weight: float) -> bool:
    """Watch-state verdict signal (completed +0.5, bounce -0.7, …).

    Returns True on success so watch_state can mark feedback_pushed.
    """
    try:
        await record_interactions([{
            "kind": "signal", "user_id": user_id, "item_id": item_id,
            "weight": weight, "timestamp": datetime.utcnow(),
        }])
    except Exception as exc:  # noqa: BLE001
        logger.warning("recommender: signal store failed for %s/%s: %s",
                       user_id, item_id, exc)
        return False

    if _backend() == "recombee":
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        if recombee.available:
            try:
                import asyncio
                rq = recombee._rq  # noqa: SLF001
                await asyncio.to_thread(
                    recombee._client.send,  # noqa: SLF001
                    rq.AddRating(user_id, item_id, max(-1.0, min(1.0, weight)),
                                 cascade_create=True),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("recommender: recombee signal mirror failed: %s", exc)
                return False
    return True


async def add_negative_interaction(user_id: str, item_id: str) -> None:
    """'Never show me this' — from Ask Reclio dislikes/blocks."""
    try:
        await record_interactions([{
            "kind": "block", "user_id": user_id, "item_id": item_id,
            "weight": -1.0, "timestamp": datetime.utcnow(),
        }])
    except Exception as exc:  # noqa: BLE001
        logger.warning("recommender: block store failed: %s", exc)

    if _backend() == "recombee":
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        if recombee.available:
            await recombee.add_negative_interaction(user_id, item_id)


# ============================================================
# Recommendation reads
# ============================================================


async def _load_user_state(user_id: str) -> tuple[list[Interaction], set[str]]:
    """Return (all interactions, excluded item ids) for the user.

    Excluded = watched (view/rating) + blocked, plus blocked_titles from
    preferences (chat blocks store the title there even when the item
    never appeared in history).
    """
    async with session_scope() as session:
        result = await session.execute(
            select(Interaction).where(Interaction.user_id == user_id)
        )
        inters = list(result.scalars().all())
        prefs = await session.get(UserPreferences, user_id)

    exclude: set[str] = set()
    for i in inters:
        if i.kind in ("view", "rating", "block"):
            exclude.add(i.item_id)
    for b in (prefs.blocked_titles if prefs else None) or []:
        tmdb_id = b.get("tmdb_id")
        kind = b.get("kind")
        if tmdb_id and kind in ("movie", "tv"):
            exclude.add(f"{kind}_{tmdb_id}")
    return inters, exclude


async def _profile_vector(inters: list[Interaction]):
    """Weighted mean of interaction-item embeddings, or None."""
    if not inters:
        return None
    vecs = await similarity.vectors_for({i.item_id for i in inters})
    if not vecs:
        return None
    import numpy as np
    profile = None
    for i in inters:
        vec = vecs.get(i.item_id)
        if vec is None:
            continue
        w = _KIND_FACTOR.get(i.kind, 1.0) * float(i.weight) * _recency_decay(i.happened_at)
        if w == 0.0:
            continue
        contrib = w * vec
        profile = contrib if profile is None else profile + contrib
    if profile is None or float(np.linalg.norm(profile)) == 0.0:
        return None
    return profile


async def top_popular(
    count: int = 50,
    *,
    exclude: Iterable[str] | None = None,
    media_type: str | None = None,
) -> list[str]:
    """Popularity-ranked catalog slice — cold-start / fill-tail fallback.

    Works even with embeddings disabled: it's a plain SQL ranking over
    content_sync's TMDB-sourced catalog.
    """
    exclude_set = set(exclude or [])
    async with session_scope() as session:
        q = (
            select(ContentCatalog.tmdb_id)
            .where(ContentCatalog.vote_average >= _FALLBACK_MIN_VOTE)
            .order_by(ContentCatalog.popularity.desc())
            .limit(count + len(exclude_set) + 50)
        )
        if media_type:
            q = q.where(ContentCatalog.media_type == media_type)
        ids = [row for row in (await session.execute(q)).scalars()]
    return [i for i in ids if i not in exclude_set][:count]


async def recommend_for_user(
    user_id: str,
    count: int = 50,
    media_type: str | None = None,
) -> list[str]:
    """Personalized 'Recommended For You' ranking (local engine).

    Profile-vector scoring when the user has embedded history;
    popularity fallback otherwise. Never returns watched/blocked items.
    """
    inters, exclude = await _load_user_state(user_id)
    profile = await _profile_vector(inters)

    ranked: list[str] = []
    if profile is not None:
        ranked = await similarity.rank_by_vector(
            profile, k=count, exclude=exclude, media_type=media_type,
            popularity_weight=_POPULARITY_WEIGHT,
        )

    if len(ranked) < count:
        seen = set(ranked) | exclude
        filler = await top_popular(count - len(ranked), exclude=seen, media_type=media_type)
        ranked.extend(filler)
    return ranked[:count]


async def recommend_similar_items(
    item_id: str,
    user_id: str,
    count: int = 25,
    media_type: str | None = None,
) -> list[str]:
    """'Because You Watched X' — semantic neighbors of the anchor,
    minus everything the user has already seen or blocked."""
    _, exclude = await _load_user_state(user_id)
    return await similarity.similar_to(
        item_id, k=count, exclude=exclude, media_type=media_type,
    )


# ============================================================
# Backend dispatch — what user_sync actually calls
# ============================================================


async def get_recommendations(
    user_id: str,
    count: int = 50,
    media_type: str | None = None,
) -> list[str]:
    if _backend() == "recombee":
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        if recombee.available:
            recs = await recombee.get_recommendations(
                user_id, count=count, filter_media_type=media_type,
            )
            if recs:
                return recs
        # Recombee configured but unavailable/empty → local still answers.
    return await recommend_for_user(user_id, count=count, media_type=media_type)


async def get_item_recommendations(
    item_id: str,
    user_id: str,
    count: int = 25,
    media_type: str | None = None,
) -> list[str]:
    if _backend() == "recombee":
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        if recombee.available:
            recs = await recombee.recommend_items_to_item(
                item_id, user_id, count=count, filter_media_type=media_type,
            )
            if recs:
                return recs
    return await recommend_similar_items(
        item_id, user_id, count=count, media_type=media_type,
    )


async def engine_status() -> dict[str, Any]:
    """Non-sensitive snapshot for /health and diagnostics."""
    from sqlalchemy import func

    backend = _backend()
    out: dict[str, Any] = {"backend": backend}
    try:
        async with session_scope() as session:
            out["catalog_items"] = await session.scalar(
                select(func.count()).select_from(ContentCatalog)
            ) or 0
            out["catalog_embedded"] = await session.scalar(
                select(func.count()).select_from(ContentCatalog)
                .where(ContentCatalog.embedding.is_not(None))
            ) or 0
            out["interactions"] = await session.scalar(
                select(func.count()).select_from(Interaction)
            ) or 0
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:160]
    if backend == "recombee":
        from app.services.recombee import get_recombee
        out["recombee_available"] = get_recombee().available
    return out
