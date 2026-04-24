from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TasteCache(Base):
    __tablename__ = "taste_cache"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    movie_genre_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    show_genre_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    last_watched_movie_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_watched_movie_title: Mapped[str | None] = mapped_column(String, nullable=True)
    last_watched_show_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_watched_show_title: Mapped[str | None] = mapped_column(String, nullable=True)

    top_actors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    top_directors: Mapped[list | None] = mapped_column(JSON, nullable=True)

    preferred_decade: Mapped[int | None] = mapped_column(Integer, nullable=True)

    total_movies_watched: Mapped[int] = mapped_column(Integer, default=0)
    total_shows_watched: Mapped[int] = mapped_column(Integer, default=0)

    computed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=True)
