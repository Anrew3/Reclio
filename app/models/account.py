"""Account: the login identity. Owns one or more `User` rows (Trakt-connected
members). A single-user setup has one Account → one User; family mode lets
one Account own multiple Users (Phase 9+).

The session cookie signs `Account.id` — never `User.id` directly — so that
member switching doesn't require re-auth and so that a signed-out-from-Trakt
member can still be swapped out without losing the login.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    # FK resolved lazily — Users are created immediately after an Account on
    # OAuth sign-in, and this pointer is set in the same transaction.
    primary_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
