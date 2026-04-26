"""Per-watch lifecycle tracking for the v1.5 watch-state machine.

Each row tracks ONE attempt by ONE user to watch ONE specific item
(movie or episode). Created when we first observe the item in
Trakt's /sync/playback, updated on every subsequent sync until a
verdict is reached.

The state machine in app/jobs/watch_state.py classifies each open
attempt as one of:
  in_progress             still watching
  completed               finished (>=90% or appears in history)
  abandoned_sleep         late-night drop, 5+ days no resume
  abandoned_bounce        daytime drop, 24h+ no resume
  abandoned_lost_interest 2+ seasons in then stopped (positive on genre)
  accidental              <5% progress, 24h+ no resume (no signal)

The `feedback_pushed` flag guards against double-counting when the
evaluator runs again after a successful Recombee + taste-profile push.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WatchAttempt(Base):
    __tablename__ = "watch_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE")
    )

    # Trakt /sync/playback assigns one stable id per in-progress entry.
    # Re-watches get a fresh id so they create a new row rather than
    # mutating a completed one.
    trakt_playback_id: Mapped[int] = mapped_column(Integer)

    # 'movie' or 'episode'
    kind: Mapped[str] = mapped_column(String(8))

    # Resolved TMDB ids — denormalized so the feedback path doesn't
    # need to re-resolve via Trakt every tick. Either movie_tmdb_id OR
    # show_tmdb_id will be populated, never both.
    movie_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    show_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Observed state. last_paused_local_hour drives the sleep heuristic
    # (22:00-04:00 in the user's local TZ counts as a likely-sleep drop).
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    last_paused_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_paused_local_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Decision
    status: Mapped[str] = mapped_column(String(24), default="in_progress")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feedback_pushed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "trakt_playback_id", name="uq_watch_attempt_user_playback"),
        Index("ix_watch_attempts_user_status", "user_id", "status"),
    )
