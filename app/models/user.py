from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    trakt_username: Mapped[str | None] = mapped_column(String, nullable=True)
    trakt_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trakt_access_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    trakt_refresh_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    trakt_token_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trakt_rec_movies_list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trakt_rec_shows_list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trakt_watchprogress_list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trakt_watchlist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_history_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    profile_ready: Mapped[bool] = mapped_column(Boolean, default=False)
