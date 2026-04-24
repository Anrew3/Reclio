"""A `User` row represents one Trakt-connected profile ("member" in product
language). An Account owns one or more Users; in the common single-user
case the Account has exactly one User.

The class name stays `User` because the ChillLink `user_id` query param
and the Recombee user ID are already keyed to this row. Renaming would
ripple into the public protocol for no gain.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    # Login identity this member belongs to. Nullable only to allow the
    # one-shot backfill on first boot of existing pre-Account installs.
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )

    # Optional family-friendly label. Defaults to trakt_username at read time.
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

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

    __table_args__ = (Index("ix_users_account_id", "account_id"),)
