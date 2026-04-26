import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def _migrate_add_user_account_columns(conn) -> None:
    """Add account_id + display_name to existing users tables.

    SQLAlchemy's `create_all` won't ALTER an existing table, and we support
    upgrading a pre-Account install without the operator having to drop the
    DB. Idempotent: checks PRAGMA first, skips if the column already exists.
    """
    rows = await conn.execute(text("PRAGMA table_info(users)"))
    existing_cols = {row[1] for row in rows.fetchall()}
    if not existing_cols:
        return  # table doesn't exist yet — create_all will handle it cleanly
    if "account_id" not in existing_cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN account_id VARCHAR"))
        logger.info("migration: added users.account_id column")
    if "display_name" not in existing_cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR"))
        logger.info("migration: added users.display_name column")
    if "recent_feed_hits" not in existing_cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN recent_feed_hits JSON"))
        logger.info("migration: added users.recent_feed_hits column")
    if "last_feed_request_at" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN last_feed_request_at DATETIME")
        )
        logger.info("migration: added users.last_feed_request_at column")
    if "trakt_byw_movies_list_id" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN trakt_byw_movies_list_id INTEGER")
        )
        logger.info("migration: added users.trakt_byw_movies_list_id column")
    if "trakt_byw_shows_list_id" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN trakt_byw_shows_list_id INTEGER")
        )
        logger.info("migration: added users.trakt_byw_shows_list_id column")


async def _migrate_add_preference_columns(conn) -> None:
    """Add `onboarding_answers` + `vibe_summary` to existing user_preferences.

    The table itself is created by `Base.metadata.create_all` for installs
    that already had v1.1.0; this migration only matters for the brief
    window between v1.1.0 and the conversational onboarding ship.
    """
    rows = await conn.execute(text("PRAGMA table_info(user_preferences)"))
    existing_cols = {row[1] for row in rows.fetchall()}
    if not existing_cols:
        return  # table doesn't exist yet — create_all will handle it cleanly
    if "onboarding_answers" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN onboarding_answers JSON")
        )
        logger.info("migration: added user_preferences.onboarding_answers column")
    if "vibe_summary" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN vibe_summary VARCHAR")
        )
        logger.info("migration: added user_preferences.vibe_summary column")
    # v1.3 fine-tune controls + chat-driven mutations
    if "pacing_preference" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN pacing_preference INTEGER DEFAULT 50")
        )
        logger.info("migration: added user_preferences.pacing_preference column")
    if "runtime_preference" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN runtime_preference INTEGER DEFAULT 50")
        )
        logger.info("migration: added user_preferences.runtime_preference column")
    if "excluded_keywords" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN excluded_keywords JSON")
        )
        logger.info("migration: added user_preferences.excluded_keywords column")
    if "boosted_keywords" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN boosted_keywords JSON")
        )
        logger.info("migration: added user_preferences.boosted_keywords column")
    if "blocked_titles" not in existing_cols:
        await conn.execute(
            text("ALTER TABLE user_preferences ADD COLUMN blocked_titles JSON")
        )
        logger.info("migration: added user_preferences.blocked_titles column")


async def _backfill_accounts_for_orphan_users() -> None:
    """For every User without an account_id, create a 1-user Account and link it.

    Runs after create_all so the accounts table exists. Idempotent: a second
    call is a no-op once the backfill has happened.
    """
    import uuid
    from datetime import datetime

    from sqlalchemy import select

    from app.models.account import Account
    from app.models.user import User

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.account_id.is_(None)))
        orphans = result.scalars().all()
        if not orphans:
            return
        for user in orphans:
            account = Account(
                id=str(uuid.uuid4()),
                primary_user_id=user.id,
                display_name=user.trakt_username,
                created_at=user.created_at or datetime.utcnow(),
                last_seen=user.last_seen,
            )
            session.add(account)
            user.account_id = account.id
        await session.commit()
        logger.info("migration: backfilled %d account(s) for legacy users", len(orphans))


async def init_db() -> None:
    # Import models so Base.metadata knows about them
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await _migrate_add_user_account_columns(conn)
        await _migrate_add_preference_columns(conn)
        await conn.run_sync(Base.metadata.create_all)

    await _backfill_accounts_for_orphan_users()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
