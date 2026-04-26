"""SQLite-backed cosine-similarity over content_catalog embeddings.

The catalog matrix loads on first call and caches in process memory
for an hour (or until invalidated by content_sync). 10k items × 768
dims = ~30 MB — fine for SQLite + RAM. Cosine of a 10k×768 matrix
against one query vector is ~30 ms in NumPy.

Public API:
    similar_to(tmdb_id_str, *, k=30, exclude=None) -> list[str]

Returns canonical Recombee-style ids ("movie_123" / "tv_456") in
descending similarity order, excluding the seed itself and any
explicitly excluded ids. Empty list when embeddings are disabled or
the seed isn't embedded yet.
"""

from __future__ import annotations

import asyncio
import logging
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
    global _matrix, _ids, _id_index, _loaded_at

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

        rows: list[tuple[str, bytes, int]] = []
        async with session_scope() as session:
            q = select(
                ContentCatalog.tmdb_id, ContentCatalog.embedding, ContentCatalog.embedding_dim
            ).where(ContentCatalog.embedding.is_not(None))
            for tmdb_id, blob, dim in (await session.execute(q)).all():
                if blob and dim:
                    rows.append((tmdb_id, blob, dim))

        if not rows:
            logger.info("similarity: no embeddings stored yet")
            return False

        # Use the most common dim; drop rows with mismatched dim (provider
        # changed mid-fleet, e.g. switched LLM_PROVIDER and not all items
        # re-embedded yet).
        from collections import Counter
        dim_mode = Counter(d for _, _, d in rows).most_common(1)[0][0]
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

        _matrix = mat
        _ids = ids
        _id_index = {i: n for n, i in enumerate(ids)}
        _loaded_at = time.monotonic()
        logger.info(
            "similarity: loaded %d embeddings × %d dims (%.1f MB)",
            mat.shape[0], mat.shape[1], mat.nbytes / 1e6,
        )
        return True


def invalidate() -> None:
    """Drop the cached matrix. Called by content_sync after a rebuild."""
    global _matrix, _ids, _id_index, _loaded_at
    _matrix = None
    _ids = []
    _id_index = {}
    _loaded_at = 0.0


async def similar_to(
    seed_id: str,
    *,
    k: int = 30,
    exclude: Iterable[str] | None = None,
    media_type: str | None = None,
) -> list[str]:
    """Return up to k tmdb-style ids most similar to `seed_id`.

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

    import numpy as np
    seed_vec = _matrix[seed_idx]
    sims = _matrix @ seed_vec  # already normalized; cosine = dot

    exclude_set = set(exclude or [])
    exclude_set.add(seed_id)

    # argpartition for top-K speed when N is large.
    take = min(k * 4, sims.shape[0])  # over-fetch to absorb exclusions
    if take <= 0:
        return []
    top_idx = np.argpartition(-sims, range(take))[:take]
    top_idx = top_idx[np.argsort(-sims[top_idx])]

    out: list[str] = []
    for idx in top_idx:
        cid = _ids[int(idx)]
        if cid in exclude_set:
            continue
        if media_type and not cid.startswith(f"{media_type}_"):
            continue
        out.append(cid)
        if len(out) >= k:
            break
    return out
