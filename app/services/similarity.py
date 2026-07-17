"""SQLite-backed cosine-similarity over content_catalog embeddings.

The catalog matrix loads on first call and caches in process memory
for an hour (or until invalidated by content_sync). 10k items × 768
dims = ~30 MB — fine for SQLite + RAM. Cosine of a 10k×768 matrix
against one query vector is ~30 ms in NumPy.

Public API:
    similar_to(seed_id, *, k=30, exclude=None, media_type=None) -> list[str]
    rank_by_vector(vec, *, k, exclude, media_type, popularity_weight) -> list[str]
    vectors_for(item_ids) -> dict[str, ndarray]

All functions return canonical catalog ids ("movie_123" / "tv_456") in
descending score order. Empty results when embeddings are disabled or
nothing is embedded yet — callers fall back gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Iterable

from sqlalchemy import select

from app.database import session_scope
from app.models.content import ContentCatalog

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 3600.0

# Module-level cache. Holds the loaded matrix + id list so repeat
# queries within the TTL skip the SQLite + numpy round trip.
_matrix = None        # numpy.ndarray, shape (N, dim), dtype float32, L2-normalized
_ids: list[str] = []  # parallel id list (e.g. "movie_550")
_id_index: dict[str, int] = {}
_pop = None           # numpy.ndarray, shape (N,), popularity normalized to [0, 1]
_quality = None       # numpy.ndarray, shape (N,), vote_average mapped to [0, 1]
_loaded_at: float = 0.0
_lock = asyncio.Lock()


def _unpack(blob: bytes, dim: int):
    """Bytes → float32 numpy array, length checked."""
    import numpy as np
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.shape[0] != dim:
        raise ValueError(f"embedding length mismatch: got {arr.shape[0]}, expected {dim}")
    return arr


async def _load_matrix() -> bool:
    """Load all stored embeddings into a single dense matrix.

    Returns True on success, False when no embeddings are available
    (e.g. embeddings disabled, or content_sync hasn't run yet).
    Idempotent + lock-protected.
    """
    global _matrix, _ids, _id_index, _pop, _quality, _loaded_at

    if _matrix is not None and (time.monotonic() - _loaded_at) < _CACHE_TTL_SEC:
        return True

    async with _lock:
        if _matrix is not None and (time.monotonic() - _loaded_at) < _CACHE_TTL_SEC:
            return True

        try:
            import numpy as np
        except ImportError:
            logger.warning("similarity: numpy not available; disabling.")
            return False

        rows: list[tuple[str, bytes, int, float, float]] = []
        async with session_scope() as session:
            q = select(
                ContentCatalog.tmdb_id, ContentCatalog.embedding,
                ContentCatalog.embedding_dim, ContentCatalog.popularity,
                ContentCatalog.vote_average,
            ).where(ContentCatalog.embedding.is_not(None))
            for tmdb_id, blob, dim, popularity, vote in (await session.execute(q)).all():
                if blob and dim:
                    rows.append((tmdb_id, blob, dim,
                                 float(popularity or 0.0), float(vote or 0.0)))

        if not rows:
            logger.info("similarity: no embeddings stored yet")
            return False

        # Use the most common dim; drop rows with mismatched dim (provider
        # changed mid-fleet, e.g. switched LLM_PROVIDER and not all items
        # re-embedded yet).
        from collections import Counter
        dim_mode = Counter(d for _, _, d, _, _ in rows).most_common(1)[0][0]
        rows = [r for r in rows if r[2] == dim_mode]

        ids = [r[0] for r in rows]
        try:
            mat = np.stack([_unpack(r[1], dim_mode) for r in rows]).astype(np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.warning("similarity: matrix stack failed: %s", exc)
            return False

        # L2-normalize so cosine similarity = mat @ q_norm.
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms

        # Popularity prior: log-scaled then min-max to [0, 1] so a blended
        # score of `cosine + w * pop` stays comparable across catalogs.
        pop = np.array([math.log1p(max(0.0, r[3])) for r in rows], dtype=np.float32)
        span = float(pop.max() - pop.min())
        pop = (pop - pop.min()) / span if span > 0 else np.zeros_like(pop)

        # Quality prior: TMDB vote_average mapped so 5.0 → 0 and 9.5+ → 1.
        quality = np.clip(
            (np.array([r[4] for r in rows], dtype=np.float32) - 5.0) / 4.5,
            0.0, 1.0,
        )

        _matrix = mat
        _ids = ids
        _id_index = {i: n for n, i in enumerate(ids)}
        _pop = pop
        _quality = quality
        _loaded_at = time.monotonic()
        logger.info(
            "similarity: loaded %d embeddings × %d dims (%.1f MB)",
            mat.shape[0], mat.shape[1], mat.nbytes / 1e6,
        )
        return True


def invalidate() -> None:
    """Drop the cached matrix. Called after catalog writes add embeddings."""
    global _matrix, _ids, _id_index, _pop, _quality, _loaded_at
    _matrix = None
    _ids = []
    _id_index = {}
    _pop = None
    _quality = None
    _loaded_at = 0.0


def _rank(scores, *, k: int, exclude: set[str], media_type: str | None) -> list[str]:
    """Shared top-K selection over a per-item score vector."""
    import numpy as np
    take = min(k * 4 + len(exclude), scores.shape[0])  # over-fetch to absorb exclusions
    if take <= 0:
        return []
    top_idx = np.argpartition(-scores, range(min(take, scores.shape[0])))[:take]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    out: list[str] = []
    for idx in top_idx:
        cid = _ids[int(idx)]
        if cid in exclude:
            continue
        if media_type and not cid.startswith(f"{media_type}_"):
            continue
        out.append(cid)
        if len(out) >= k:
            break
    return out


async def similar_to(
    seed_id: str,
    *,
    k: int = 30,
    exclude: Iterable[str] | None = None,
    media_type: str | None = None,
) -> list[str]:
    """Return up to k catalog ids most similar to `seed_id`.

    seed_id        canonical id, e.g. "movie_27205" or "tv_1396"
    k              max results
    exclude        additional ids to filter out (e.g. user's watched set)
    media_type     'movie' | 'tv' to constrain the result set
    """
    if not await _load_matrix() or _matrix is None:
        return []
    seed_idx = _id_index.get(seed_id)
    if seed_idx is None:
        return []

    seed_vec = _matrix[seed_idx]
    sims = _matrix @ seed_vec  # already normalized; cosine = dot

    exclude_set = set(exclude or [])
    exclude_set.add(seed_id)
    return _rank(sims, k=k, exclude=exclude_set, media_type=media_type)


async def rank_by_vector(
    vec,
    *,
    k: int = 50,
    exclude: Iterable[str] | None = None,
    media_type: str | None = None,
    popularity_weight: float = 0.0,
) -> list[str]:
    """Rank the whole catalog against an arbitrary query vector.

    Used by the local recommender: `vec` is a user's taste-profile
    vector (weighted mean of watched-item embeddings). An optional
    popularity prior keeps results from drifting too deep into the
    long tail: score = cosine + popularity_weight × pop_norm.
    """
    if not await _load_matrix() or _matrix is None:
        return []
    import numpy as np

    q = np.asarray(vec, dtype=np.float32)
    if q.shape[0] != _matrix.shape[1]:
        logger.debug(
            "rank_by_vector: dim mismatch (query %d vs matrix %d)",
            q.shape[0], _matrix.shape[1],
        )
        return []
    norm = float(np.linalg.norm(q))
    if norm == 0:
        return []
    q = q / norm

    scores = _matrix @ q
    if popularity_weight and _pop is not None:
        scores = scores + popularity_weight * _pop

    return _rank(scores, k=k, exclude=set(exclude or []), media_type=media_type)


async def vectors_for(item_ids: Iterable[str]) -> dict:
    """Return {item_id: L2-normalized ndarray} for every id in the matrix."""
    if not await _load_matrix() or _matrix is None:
        return {}
    out = {}
    for item_id in item_ids:
        idx = _id_index.get(item_id)
        if idx is not None:
            out[item_id] = _matrix[idx]
    return out


async def catalog_scores(
    facet_vectors: list,
    *,
    negative_vector=None,
    negative_weight: float = 0.35,
    popularity_weight: float = 0.0,
    quality_weight: float = 0.0,
    bias_vectors: list | None = None,
):
    """Score every catalog item against a multi-facet taste profile.

    score_i = max_f cos(facet_f, item_i)
              − negative_weight × max(0, cos(neg, item_i))
              + popularity_weight × pop_norm_i
              + quality_weight × vote_norm_i
              + Σ_b  w_b × cos(bias_b, item_i)      (preference sliders)

    Max-over-facets is the point: a viewer with a drama facet AND an
    action facet gets strong scores near *both*, instead of the mushy
    midpoint a single mean vector produces.

    Returns (ids, scores) — a shared reference to the catalog id list
    plus a float32 score array aligned to it — or (None, None) when no
    embeddings are loaded. Callers must not mutate `ids`.
    """
    if not facet_vectors or not await _load_matrix() or _matrix is None:
        return None, None
    import numpy as np

    scores = None
    for vec in facet_vectors:
        q = np.asarray(vec, dtype=np.float32)
        if q.shape[0] != _matrix.shape[1]:
            continue
        norm = float(np.linalg.norm(q))
        if norm == 0:
            continue
        sims = _matrix @ (q / norm)
        scores = sims if scores is None else np.maximum(scores, sims)
    if scores is None:
        return None, None

    if negative_vector is not None:
        q = np.asarray(negative_vector, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0 and q.shape[0] == _matrix.shape[1]:
            neg_sims = _matrix @ (q / norm)
            scores = scores - negative_weight * np.clip(neg_sims, 0.0, None)

    if popularity_weight and _pop is not None:
        scores = scores + popularity_weight * _pop

    if quality_weight and _quality is not None:
        scores = scores + quality_weight * _quality

    # Signed semantic biases — each (vector, weight) nudges the whole
    # ranking along an embedding direction (preference sliders).
    for bias_vec, bias_w in (bias_vectors or []):
        if not bias_w:
            continue
        q = np.asarray(bias_vec, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0 and q.shape[0] == _matrix.shape[1]:
            scores = scores + float(bias_w) * (_matrix @ (q / norm))

    return _ids, scores
