"""Offline evaluation harness for the recommendation engine.

Leave-last-N-out backtest: for every user with enough history, hide
their most recent N embedded watches, rebuild the taste profile from
what's left, and check whether the engine would have recommended the
hidden items. Because everything is local, this runs in-process in
seconds — no service calls, no cost.

Metrics per user:
    hits          how many held-out items landed in the top-K
    recall@K      hits / holdout size
Aggregate:
    hit_rate      fraction of users with ≥1 hit
    mean_recall   average recall@K across users

Exposed via GET /admin/eval. Use it to judge every ranking change —
facet counts, half-life, popularity weight, MMR diversity — with a
number instead of a feeling.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.database import session_scope
from app.models.feedback import RecFeedback
from app.models.interaction import Interaction
from app.services import recommender

logger = logging.getLogger(__name__)

_MIN_HISTORY = 12   # users with fewer embedded positives are skipped
_MAX_USERS = 50     # bound the sweep


async def evaluate(k: int = 50, holdout: int = 5) -> dict[str, Any]:
    """Run the backtest across all eligible users. Returns aggregate +
    per-user detail, JSON-serializable."""
    t0 = time.monotonic()
    k = max(5, min(200, k))
    holdout = max(1, min(20, holdout))

    async with session_scope() as session:
        result = await session.execute(select(Interaction))
        all_inters = list(result.scalars().all())
        fb_result = await session.execute(select(RecFeedback))
        all_feedback = list(fb_result.scalars().all())

    by_user: dict[str, list[Interaction]] = {}
    for i in all_inters:
        by_user.setdefault(i.user_id, []).append(i)
    fb_by_user: dict[str, list[RecFeedback]] = {}
    for f in all_feedback:
        fb_by_user.setdefault(f.user_id, []).append(f)

    # Which items actually have embeddings? A held-out item the engine
    # can't even see would poison recall.
    from app.services.similarity import vectors_for
    all_item_ids = {i.item_id for i in all_inters}
    embedded = set((await vectors_for(all_item_ids)).keys())

    users_evaluated = 0
    users_with_hit = 0
    recalls: list[float] = []
    per_user: list[dict[str, Any]] = []

    for user_id, inters in list(by_user.items())[:_MAX_USERS]:
        positives = [
            i for i in inters
            if i.kind in ("view", "rating") and i.weight > 0
            and i.item_id in embedded and i.happened_at is not None
        ]
        if len(positives) < _MIN_HISTORY + holdout:
            continue

        positives.sort(key=lambda i: i.happened_at)
        held = positives[-holdout:]
        held_ids = {h.item_id for h in held}
        train = [i for i in inters if i.item_id not in held_ids]

        train_watched = {
            i.item_id for i in train if i.kind in ("view", "rating", "block")
        }

        recs = await recommender.rank_for_interactions(
            train, fb_by_user.get(user_id, []),
            exclude=train_watched,
            count=k,
            media_type=None,
            diversity=0.25,
        )
        hits = len(held_ids & set(recs))
        recall = hits / len(held_ids)

        users_evaluated += 1
        users_with_hit += 1 if hits else 0
        recalls.append(recall)
        per_user.append({
            "user_id": user_id,
            "history_size": len(positives),
            "holdout": len(held_ids),
            "hits": hits,
            "recall": round(recall, 3),
        })

    aggregate = {
        "users_evaluated": users_evaluated,
        "k": k,
        "holdout": holdout,
        "hit_rate": round(users_with_hit / users_evaluated, 3) if users_evaluated else None,
        "mean_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
    if not users_evaluated:
        aggregate["note"] = (
            f"No users with ≥{_MIN_HISTORY + holdout} embedded, timestamped "
            "positive interactions yet — sync more history first."
        )
    return {"aggregate": aggregate, "per_user": per_user}
