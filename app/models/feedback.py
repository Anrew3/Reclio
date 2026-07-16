"""Feedback-loop tables: what the engine served, and what users said.

RecommendationEvent — one row per (user, item) per serve batch. Lets the
engine decay items it keeps showing that the user never engages with,
and gives the eval harness production ground truth.

RecFeedback — free-text comments from the /recommendations page
("loved the pacing", "too bleak, not for me"). Stored with the LLM's
structured parse AND an embedding of the comment text itself — the
embedding lives in the same vector space as the catalog, so the comment
steers the taste profile directly even when the LLM is offline.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime, Float, Index, Integer, JSON, LargeBinary, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RecommendationEvent(Base):
    __tablename__ = "recommendation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)  # "movie_550"
    media_type: Mapped[str] = mapped_column(String)               # "movie" | "tv"
    rank: Mapped[int] = mapped_column(Integer, default=0)
    served_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_rec_events_user_item", "user_id", "item_id"),
        Index("ix_rec_events_served_at", "served_at"),
    )


class RecFeedback(Base):
    __tablename__ = "rec_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)  # None for quick 👍/👎
    # -1.0 … +1.0. From the LLM parse, the heuristic fallback, or ±0.8
    # for quick reactions.
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full structured parse: {source, liked, disliked, actions...}
    parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Embedding of the comment text (same provider/space as the catalog).
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_rec_feedback_user", "user_id"),
    )
