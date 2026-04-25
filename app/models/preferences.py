"""User preferences captured via the onboarding questionnaire.

Kept in its own table so updates don't churn the User row (which is touched
on every /feeds request via `last_feed_request_at`). One row per User —
absence of a row means "preferences never set".
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # Has the user completed the onboarding questionnaire? Drives the
    # one-time redirect from /dashboard → /onboarding.
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    # 0..100 sliders. 50 = neutral default that matches today's behavior.
    #   discovery_level: 0 = only popular, 100 = chase hidden gems
    #   era_preference:  0 = love classics, 100 = only new
    discovery_level: Mapped[int] = mapped_column(Integer, default=50)
    era_preference: Mapped[int] = mapped_column(Integer, default=50)

    # Genre IDs (TMDB) the user wants completely filtered out.
    # Lists are intentionally separate per media type because TMDB uses
    # different ID spaces (e.g. 10759 = TV "Action & Adventure" doesn't
    # exist on movies).
    excluded_movie_genres: Mapped[list | None] = mapped_column(JSON, nullable=True)
    excluded_show_genres: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Mood tags inferred by the LLM from the conversational onboarding
    # answers. The fixed palette lives in app.routers.onboarding.MOODS.
    favorite_moods: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Hide adult / mature-rated content. Maps to TMDB's `include_adult=false`
    # plus a `certification.lte=PG-13` floor on /discover queries.
    family_safe: Mapped[bool] = mapped_column(Boolean, default=False)

    # Raw open-ended answers from the conversational onboarding UI.
    # Stored verbatim (sanitized for length) so we can re-derive preferences
    # if the LLM extraction prompt evolves. Shape: {q_key: "answer text"}.
    onboarding_answers: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # One-paragraph LLM-written summary of the viewer's taste. Reused by
    # Ask Reclio for context grounding. NULL until first onboarding pass.
    vibe_summary: Mapped[str | None] = mapped_column(String, nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
