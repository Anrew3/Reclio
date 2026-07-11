"""Local interaction store — the data the recommendation engine learns from.

One row per (user, item, kind) with the most recent weight + timestamp.
Trakt history is re-fetched every sync, so upsert semantics keep the
table bounded (~items-touched × 4 kinds) while always reflecting the
latest signal. This is the local replacement for the interaction data
that used to live only inside Recombee.

Kinds:
    view      — appeared in Trakt watch history          (weight  1.0)
    rating    — explicit Trakt rating                    (weight -1.0 … 1.0)
    bookmark  — on the Trakt watchlist                   (weight  0.7)
    signal    — watch-state verdict (completed/bounce/…) (weight -1.0 … 1.0)
    block     — "never show me this" from Ask Reclio     (weight -1.0)
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical catalog id, e.g. "movie_550" / "tv_1396"
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    # When the underlying event happened (watched_at / rated_at / …).
    # Drives recency decay in the profile vector.
    happened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "kind", name="uq_interaction"),
        Index("ix_interactions_user_id", "user_id"),
    )
