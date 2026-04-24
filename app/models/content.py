from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentCatalog(Base):
    __tablename__ = "content_catalog"

    tmdb_id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. "movie_550"
    media_type: Mapped[str] = mapped_column(String)  # "movie" or "tv"
    title: Mapped[str] = mapped_column(String)
    overview: Mapped[str | None] = mapped_column(String, nullable=True)
    genres: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cast: Mapped[list | None] = mapped_column(JSON, nullable=True)
    director: Mapped[str | None] = mapped_column(String, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vote_average: Mapped[float | None] = mapped_column(Float, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_stored: Mapped[bool] = mapped_column(Boolean, default=False)
    recombee_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
