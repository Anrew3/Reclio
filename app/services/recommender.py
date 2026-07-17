"""Local-first recommendation engine.

This module is the single seam every caller goes through for
recommendation reads and interaction writes. The backend is picked by
`RECOMMENDER` (settings.recommender):

    local     (default) — fully self-contained. Interactions live in the
                `interactions` SQLite table; recommendations come from
                multi-facet taste profiles scored against the catalog
                embedding matrix (see similarity.py). No third-party
                recommendation API.
    recombee  (legacy) — proxies to the Recombee SaaS as pre-1.7
                versions did. Requires the recombee-api-client package
                and RECOMBEE_* env vars.

Interactions are ALWAYS recorded locally, even in recombee mode, so an
instance can switch to `local` later without losing its history.

Ranking pipeline (local mode, v1.8):

  1. FACETS — the user's positively-weighted items (plus positive
     comment embeddings from the /recommendations page) are k-means
     clustered into up to 4 "taste facets". Each catalog item scores
     against the *nearest* facet, so a viewer who loves both quiet
     dramas and loud action gets strong picks near both poles instead
     of mush at the midpoint. Negative signal (blocks, bounces, low
     ratings, critical comments) forms a separate repulsion vector.

  2. PRIORS & DECAY — a small popularity prior keeps the ranking out of
     the obscurity tail; items the engine has served repeatedly without
     any engagement decay a little more on every serve.

  3. MMR RE-RANK — the final list is assembled greedily with maximal
     marginal relevance: each pick is penalized by its similarity to
     already-picked items. The user's `discovery_level` preference
     drives the diversity weight.

Cold start falls back to a quality-floored popularity ranking, so the
row is never empty.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.database import session_scope
from app.models.content import ContentCatalog
from app.models.feedback import RecFeedback, RecommendationEvent
from app.models.interaction import Interaction
from app.models.preferences import UserPreferences
from app.services import similarity

logger = logging.getLogger(__name__)

# Contribution multiplier per interaction kind. `rating`, `signal`,
# `feedback` and `block` rows carry their own signed weight in [-1, 1];
# these factors set how loudly each kind speaks relative to a plain view.
_KIND_FACTOR: dict[str, float] = {
    "view": 1.0,
    "rating": 1.6,     # explicit opinion beats implicit watch
    "bookmark": 0.7,   # intent, not experience
    "signal": 1.2,     # watch-state verdicts (completed / abandoned_*)
    "feedback": 1.7,   # considered, written-down opinion from the recs page
    "block": 1.8,      # "never again" — loudest negative
}

# Comment embeddings steer the profile at this multiple of |sentiment|.
_COMMENT_FACTOR = 1.5

# Recency half-life for the profile vector. Shorter than the taste
# profile's 2 years: the rec row should chase current mood.
_PROFILE_HALF_LIFE_DAYS = 270.0

# Popularity prior weight in the blended score.
_POPULARITY_WEIGHT = 0.15

# Repulsion strength of the negative vector.
_NEGATIVE_WEIGHT = 0.35

# Per-serve decay for items shown but never engaged with, and its cap.
_SERVE_PENALTY = 0.035
_SERVE_PENALTY_CAP = 5

# Serve events older than this are pruned (and stop counting).
_SERVE_RETENTION_DAYS = 90

# Max taste facets. Below ~6 positive items everything is one facet.
_MAX_FACETS = 4
_ITEMS_PER_FACET = 6

# Quality floor for the popularity fallback so cold-start rows aren't
# filled with high-popularity shovelware.
_FALLBACK_MIN_VOTE = 6.0


def _backend() -> str:
    return get_settings().recommender


def _recency_decay(
    happened_at: datetime | None,
    half_life_days: float = _PROFILE_HALF_LIFE_DAYS,
) -> float:
    if happened_at is None:
        return 1.0
    age_days = max(0.0, (datetime.utcnow() - happened_at).total_seconds() / 86400.0)
    return math.pow(2.0, -age_days / half_life_days)


# ============================================================
# Preference sliders → engine parameters
# ============================================================

# Semantic-anchor sliders: each biases the ranking along the embedding
# direction between two pole descriptions. The pole texts are embedded
# once per provider and cached.
_ANCHOR_SLIDERS: dict[str, tuple[str, str]] = {
    "tone_preference": (
        "dark gritty bleak disturbing grim tragedy",
        "light fun feel-good heartwarming uplifting charming",
    ),
    "intensity_preference": (
        "cozy gentle calm relaxing comforting low-stakes",
        "intense thrilling suspenseful edge-of-your-seat adrenaline",
    ),
    "complexity_preference": (
        "simple accessible easy-watching crowd-pleaser straightforward",
        "cerebral complex layered thought-provoking philosophical ambiguous",
    ),
    "humor_preference": (
        "serious dramatic somber earnest heavy",
        "hilarious funny comedy witty laugh-out-loud absurd",
    ),
}

# Max score nudge a semantic slider can apply at its extreme. Cosine
# scores live in roughly [-1, 1], so 0.12 shifts rankings noticeably
# without letting one slider steamroll the taste profile.
_ANCHOR_MAX_WEIGHT = 0.12

# Module cache: (provider_name, text) → normalized ndarray | None.
_anchor_cache: dict[tuple[str, str], Any] = {}


async def _anchor_vector(text: str):
    """Embed a pole description into catalog space (cached)."""
    from app.services.embeddings import embed_text, get_embeddings_provider
    key = (get_embeddings_provider().name, text)
    if key in _anchor_cache:
        return _anchor_cache[key]
    vec = None
    try:
        raw = await embed_text(text)
        if raw:
            import numpy as np
            arr = np.asarray(raw, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                vec = arr / norm
    except Exception as exc:  # noqa: BLE001
        logger.debug("recommender: anchor embed failed: %s", exc)
    _anchor_cache[key] = vec
    return vec


async def _slider_biases(prefs: UserPreferences | None) -> list[tuple[Any, float]]:
    """Active semantic sliders → [(direction_vector, signed_weight)].

    A slider at 50 is neutral (no bias). The direction is the normalized
    difference high_pole − low_pole; the weight is proportional to how
    far the slider sits from center. Skipped gracefully when embeddings
    are unavailable or the anchor dims don't match the catalog.
    """
    if prefs is None:
        return []
    import numpy as np
    biases: list[tuple[Any, float]] = []
    for field, (low_text, high_text) in _ANCHOR_SLIDERS.items():
        value = getattr(prefs, field, None)
        if value is None:
            continue
        offset = (max(0, min(100, value)) - 50) / 50.0  # -1 … +1
        if abs(offset) < 0.1:
            continue
        low_vec = await _anchor_vector(low_text)
        high_vec = await _anchor_vector(high_text)
        if low_vec is None or high_vec is None:
            continue
        direction = high_vec - low_vec
        norm = float(np.linalg.norm(direction))
        if norm == 0:
            continue
        biases.append((direction / norm, _ANCHOR_MAX_WEIGHT * offset))
    return biases


def _popularity_weight(prefs: UserPreferences | None) -> float:
    """mainstream_level 0..100 → popularity prior 0.0..0.30 (50 → 0.15)."""
    return 0.30 * (_pref_level(prefs, "mainstream_level") / 100.0)


def _quality_weight(prefs: UserPreferences | None) -> float:
    """acclaim_level 0..100 → vote-quality boost 0.0..0.24 (50 → 0.12)."""
    return 0.24 * (_pref_level(prefs, "acclaim_level") / 100.0)


def _half_life_days(prefs: UserPreferences | None) -> float:
    """memory_horizon 0..100 → profile half-life 60..730 days.

    Exponential interpolation so the midpoint lands near the historical
    default (~270 days): 60 × (730/60)^(v/100).
    """
    return 60.0 * math.pow(730.0 / 60.0, _pref_level(prefs, "memory_horizon") / 100.0)


# ============================================================
# Interaction writes — always local, mirrored to Recombee when
# that backend is active.
# ============================================================


async def record_interactions(interactions: list[dict[str, Any]]) -> int:
    """Upsert a batch of interactions into the local store.

    Each dict: {"kind", "user_id", "item_id", "weight"?, "timestamp"?}
    (the same shape user_sync builds, plus "rating" is accepted as an
    alias for "weight" on rating rows). Returns rows written.
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
# Serve tracking — feed the served-but-ignored decay + eval
# ============================================================


async def log_served(
    user_id: str,
    items: Iterable[tuple[str, str]],
    *,
    limit_per_media: int = 30,
) -> None:
    """Record what the engine served (item_id, media_type) this batch.

    Also prunes events past the retention window so the table — and the
    decay it drives — stays bounded.
    """
    now = datetime.utcnow()
    rows = []
    per_media: dict[str, int] = {}
    for rank, (item_id, media_type) in enumerate(items):
        n = per_media.get(media_type, 0)
        if n >= limit_per_media:
            continue
        per_media[media_type] = n + 1
        rows.append(RecommendationEvent(
            user_id=user_id, item_id=item_id, media_type=media_type,
            rank=rank, served_at=now,
        ))
    try:
        async with session_scope() as session:
            if rows:
                session.add_all(rows)
            await session.execute(
                delete(RecommendationEvent).where(
                    RecommendationEvent.served_at
                    < now - timedelta(days=_SERVE_RETENTION_DAYS)
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("recommender: serve log failed for %s: %s", user_id, exc)


async def _serve_counts(user_id: str) -> dict[str, int]:
    """How many times each item was served to this user (retention window)."""
    from sqlalchemy import func
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(RecommendationEvent.item_id, func.count())
                .where(RecommendationEvent.user_id == user_id)
                .group_by(RecommendationEvent.item_id)
            )
            return {item_id: int(n) for item_id, n in result.all()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("recommender: serve counts failed: %s", exc)
        return {}


# ============================================================
# Profile state
# ============================================================


async def _load_user_state(
    user_id: str,
) -> tuple[list[Interaction], list[RecFeedback], set[str], UserPreferences | None]:
    """Return (interactions, comment feedback, excluded ids, prefs).

    Excluded = watched (view/rating) + blocked + strongly-disliked
    feedback, plus blocked_titles from preferences.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(Interaction).where(Interaction.user_id == user_id)
        )
        inters = list(result.scalars().all())
        fb_result = await session.execute(
            select(RecFeedback).where(RecFeedback.user_id == user_id)
        )
        feedback = list(fb_result.scalars().all())
        prefs = await session.get(UserPreferences, user_id)

    exclude: set[str] = set()
    for i in inters:
        if i.kind in ("view", "rating", "block"):
            exclude.add(i.item_id)
        elif i.kind == "feedback" and i.weight <= -0.6:
            exclude.add(i.item_id)
    for b in (prefs.blocked_titles if prefs else None) or []:
        tmdb_id = b.get("tmdb_id")
        kind = b.get("kind")
        if tmdb_id and kind in ("movie", "tv"):
            exclude.add(f"{kind}_{tmdb_id}")
    return inters, feedback, exclude, prefs


def _unpack_vec(blob: bytes, dim: int):
    import numpy as np
    arr = np.frombuffer(blob, dtype=np.float32)
    return arr if arr.shape[0] == dim else None


async def _weighted_vectors(
    inters: list[Interaction],
    feedback: list[RecFeedback],
    half_life_days: float = _PROFILE_HALF_LIFE_DAYS,
) -> list[tuple[Any, float]]:
    """Collect (embedding, signed contribution) pairs for the profile.

    Item interactions use the catalog embedding; written comments use
    the embedding of the comment text itself — same vector space, so
    "loved the slow-burn tension" literally pulls the profile toward
    slow-burn-tension content.
    """
    pairs: list[tuple[Any, float]] = []

    vecs = await similarity.vectors_for({i.item_id for i in inters})
    expected_dim = None
    for v in vecs.values():
        expected_dim = v.shape[0]
        break

    for i in inters:
        vec = vecs.get(i.item_id)
        if vec is None:
            continue
        w = (_KIND_FACTOR.get(i.kind, 1.0) * float(i.weight)
             * _recency_decay(i.happened_at, half_life_days))
        if w != 0.0:
            pairs.append((vec, w))

    import numpy as np
    for fb in feedback:
        if not fb.embedding or not fb.embedding_dim or fb.sentiment is None:
            continue
        if expected_dim is not None and fb.embedding_dim != expected_dim:
            continue  # embedding provider changed since the comment was stored
        vec = _unpack_vec(fb.embedding, fb.embedding_dim)
        if vec is None:
            continue
        norm = float(np.linalg.norm(vec))
        if norm == 0:
            continue
        w = (_COMMENT_FACTOR * float(fb.sentiment)
             * _recency_decay(fb.created_at, half_life_days))
        if w != 0.0:
            pairs.append((vec / norm, w))

    return pairs


def _kmeans_labels(mat, k: int, iters: int = 12):
    """Tiny deterministic k-means on L2-normalized rows (cosine space).

    Farthest-point init from row 0 — no randomness, so recommendations
    are stable between runs with unchanged data.
    """
    import numpy as np
    n = mat.shape[0]
    k = min(k, n)
    centers = [0]
    for _ in range(1, k):
        sims = np.max(mat @ mat[centers].T, axis=1)
        centers.append(int(np.argmin(sims)))
    C = mat[centers].copy()
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        labels = np.argmax(mat @ C.T, axis=1)
        for j in range(k):
            members = mat[labels == j]
            if len(members):
                c = members.mean(axis=0)
                nrm = float(np.linalg.norm(c))
                if nrm > 0:
                    C[j] = c / nrm
    return labels


def _build_facets(pairs: list[tuple[Any, float]]):
    """Split weighted vectors into positive taste facets + one negative
    repulsion vector. Returns (facets: list[vec], neg: vec | None)."""
    import numpy as np

    pos = [(v, w) for v, w in pairs if w > 0]
    neg = [(v, -w) for v, w in pairs if w < 0]

    facets: list[Any] = []
    if pos:
        mat = np.stack([v for v, _ in pos]).astype(np.float32)
        weights = np.array([w for _, w in pos], dtype=np.float32)
        # Ceiling division: a small outlying group (e.g. one enthusiastic
        # "more like this" comment) can still claim its own facet instead
        # of being averaged into an existing one.
        k = max(1, min(_MAX_FACETS, math.ceil(len(pos) / _ITEMS_PER_FACET)))
        if k <= 1:
            labels = np.zeros(len(pos), dtype=np.int64)
            k = 1
        else:
            labels = _kmeans_labels(mat, k)
        for j in range(k):
            mask = labels == j
            if not mask.any():
                continue
            facet = (mat[mask] * weights[mask, None]).sum(axis=0)
            nrm = float(np.linalg.norm(facet))
            if nrm > 0:
                facets.append(facet / nrm)

    neg_vec = None
    if neg:
        acc = None
        for v, w in neg:
            contrib = np.asarray(v, dtype=np.float32) * w
            acc = contrib if acc is None else acc + contrib
        if acc is not None:
            nrm = float(np.linalg.norm(acc))
            if nrm > 0:
                neg_vec = acc / nrm

    return facets, neg_vec


# ============================================================
# Ranking
# ============================================================


def _mmr_select(
    candidates: list[tuple[str, float]],
    vecs: dict[str, Any],
    count: int,
    diversity: float,
) -> list[str]:
    """Greedy maximal-marginal-relevance selection.

    pick = argmax( relevance − diversity × max_sim_to_already_picked )

    Keeps the row from being fifty flavors of the same thing. Candidates
    without a vector are appended by raw relevance at the end.
    """
    import numpy as np

    with_vec = [(cid, score) for cid, score in candidates if cid in vecs]
    without_vec = [cid for cid, _ in candidates if cid not in vecs]

    selected: list[str] = []
    selected_mat = None
    remaining = list(with_vec)
    while remaining and len(selected) < count:
        best_idx, best_val = 0, -1e9
        for idx, (cid, score) in enumerate(remaining):
            if selected_mat is None:
                val = score
            else:
                sim = float(np.max(selected_mat @ vecs[cid]))
                val = score - diversity * sim
            if val > best_val:
                best_idx, best_val = idx, val
        cid, _ = remaining.pop(best_idx)
        selected.append(cid)
        v = vecs[cid][None, :]
        selected_mat = v if selected_mat is None else np.vstack([selected_mat, v])

    for cid in without_vec:
        if len(selected) >= count:
            break
        selected.append(cid)
    return selected[:count]


def _pref_level(prefs: UserPreferences | None, field: str) -> int:
    """Slider value with None-safe default — 0 is a real value, not "unset"."""
    if prefs is None:
        return 50
    value = getattr(prefs, field, None)
    return 50 if value is None else max(0, min(100, int(value)))


def _diversity_from_prefs(prefs: UserPreferences | None) -> float:
    """discovery_level 0..100 → MMR diversity weight 0.10..0.45."""
    return 0.10 + 0.35 * (_pref_level(prefs, "discovery_level") / 100.0)


async def rank_for_interactions(
    inters: list[Interaction],
    feedback: list[RecFeedback],
    *,
    exclude: set[str],
    count: int = 50,
    media_type: str | None = None,
    diversity: float = 0.25,
    serve_counts: dict[str, int] | None = None,
    prefs: UserPreferences | None = None,
) -> list[str]:
    """Core ranking, decoupled from storage so the eval harness can
    inject held-out interaction subsets. Returns ranked item ids
    (embedding path only — no popularity fallback here).

    `prefs` feeds the engine sliders: mainstream_level (popularity
    prior), acclaim_level (quality prior), memory_horizon (profile
    half-life), and the semantic-anchor sliders (tone / intensity /
    complexity / humor). None → historical defaults.
    """
    pairs = await _weighted_vectors(inters, feedback, _half_life_days(prefs))
    if not pairs:
        return []
    facets, neg_vec = _build_facets(pairs)
    if not facets:
        return []

    ids, scores = await similarity.catalog_scores(
        facets,
        negative_vector=neg_vec,
        negative_weight=_NEGATIVE_WEIGHT,
        popularity_weight=_popularity_weight(prefs),
        quality_weight=_quality_weight(prefs),
        bias_vectors=await _slider_biases(prefs),
    )
    if ids is None:
        return []

    import numpy as np
    if serve_counts:
        # Served-but-ignored decay: every un-engaged serve nudges the
        # item down so the row rotates instead of going stale.
        penalties = np.zeros(len(ids), dtype=np.float32)
        for idx, cid in enumerate(ids):
            n = serve_counts.get(cid)
            if n:
                penalties[idx] = _SERVE_PENALTY * min(n, _SERVE_PENALTY_CAP)
        scores = scores - penalties

    # Candidate pool: over-fetch for MMR + exclusions.
    pool_size = min(len(ids), count * 4 + len(exclude))
    top_idx = np.argpartition(-scores, min(pool_size, len(ids) - 1))[:pool_size]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    candidates: list[tuple[str, float]] = []
    for idx in top_idx:
        cid = ids[int(idx)]
        if cid in exclude:
            continue
        if media_type and not cid.startswith(f"{media_type}_"):
            continue
        candidates.append((cid, float(scores[int(idx)])))
        if len(candidates) >= count * 3:
            break

    vecs = await similarity.vectors_for([cid for cid, _ in candidates])
    return _mmr_select(candidates, vecs, count, diversity)


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
    """Personalized ranking (local engine): facets + priors + MMR.

    Popularity fallback fills the tail so the row is never empty. Never
    returns watched/blocked items.
    """
    inters, feedback, exclude, prefs = await _load_user_state(user_id)
    serve_counts = await _serve_counts(user_id)

    ranked = await rank_for_interactions(
        inters, feedback,
        exclude=exclude,
        count=count,
        media_type=media_type,
        diversity=_diversity_from_prefs(prefs),
        serve_counts=serve_counts,
        prefs=prefs,
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
    """Semantic neighbors of an anchor, minus watched/blocked."""
    _, _, exclude, _ = await _load_user_state(user_id)
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
            out["feedback_comments"] = await session.scalar(
                select(func.count()).select_from(RecFeedback)
            ) or 0
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:160]
    if backend == "recombee":
        from app.services.recombee import get_recombee
        out["recombee_available"] = get_recombee().available
    return out
