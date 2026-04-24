"""Web portal routes: landing, OAuth flow, dashboard."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.account import Account
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.services.tmdb import MOVIE_GENRES, TV_GENRES
from app.services.trakt import get_trakt
from app.utils.crypto import encrypt
from app.utils.session import (
    ACTIVE_MEMBER_COOKIE,
    ACTIVE_MEMBER_MAX_AGE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_active_member_token,
    create_session_token,
    read_active_member_token,
    read_session_token,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["portal"])

_STATE_COOKIE = "reclio_oauth_state"


def _current_account_id(request: Request) -> str | None:
    """Return the authenticated Account.id from the signed session cookie."""
    return read_session_token(request.cookies.get(SESSION_COOKIE))


async def _resolve_active_user(
    request: Request, account: Account, session: AsyncSession
) -> User | None:
    """Pick which Member (User) the Account is currently 'viewing as'.

    Priority:
      1. Signed ACTIVE_MEMBER_COOKIE if it points to a User under this Account.
      2. account.primary_user_id.
      3. Any User with account_id == account.id (fallback for legacy).
    """
    candidate_id = read_active_member_token(request.cookies.get(ACTIVE_MEMBER_COOKIE))
    if candidate_id:
        user = await session.get(User, candidate_id)
        if user is not None and user.account_id == account.id:
            return user

    if account.primary_user_id:
        user = await session.get(User, account.primary_user_id)
        if user is not None and user.account_id == account.id:
            return user

    # Last resort: any member under the account
    result = await session.execute(
        select(User).where(User.account_id == account.id).limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/", response_class=HTMLResponse)
async def landing(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    # If signed in with a still-valid account, send straight to dashboard
    account_id = _current_account_id(request)
    if account_id:
        account = await session.get(Account, account_id)
        if account is not None:
            return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "settings": get_settings()},
    )


@router.get("/auth/trakt")
async def auth_trakt_start() -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    trakt = get_trakt()
    url = trakt.build_authorize_url(state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE,
        state,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,
    )
    return response


@router.get("/signin")
async def signin() -> RedirectResponse:
    """Alias that kicks off the Trakt OAuth flow for returning users."""
    return RedirectResponse(url="/auth/trakt", status_code=302)


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    if error:
        return templates.TemplateResponse(
            "callback.html",
            {"request": request, "settings": settings, "error": error},
            status_code=400,
        )

    saved_state = request.cookies.get(_STATE_COOKIE)
    if not code or not state or not saved_state or state != saved_state:
        return templates.TemplateResponse(
            "callback.html",
            {
                "request": request,
                "settings": settings,
                "error": "Invalid authorization state. Please try connecting again.",
            },
            status_code=400,
        )

    trakt = get_trakt()
    try:
        tokens = await trakt.exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OAuth token exchange failed: %s", exc)
        return templates.TemplateResponse(
            "callback.html",
            {
                "request": request,
                "settings": settings,
                "error": "Trakt rejected the authorization. Please try again.",
            },
            status_code=400,
        )

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in", 7776000))
    if not access_token or not refresh_token:
        return templates.TemplateResponse(
            "callback.html",
            {"request": request, "settings": settings, "error": "Missing tokens from Trakt."},
            status_code=400,
        )

    # Fetch profile
    try:
        profile = await trakt.get_user_profile(access_token)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Trakt profile fetch failed: %s", exc)
        profile = {}

    trakt_username = (profile.get("username") or "").lower()
    trakt_user_id = str((profile.get("ids") or {}).get("slug") or trakt_username)

    # Find existing user by username (if any). If found, reuse their Account.
    user: User | None = None
    if trakt_username:
        result = await session.execute(
            select(User).where(User.trakt_username == trakt_username)
        )
        user = result.scalar_one_or_none()

    is_new = user is None
    if user is None:
        # New Trakt user → create a fresh Account + User pair
        account = Account(
            id=str(uuid.uuid4()),
            display_name=trakt_username or None,
            last_seen=datetime.utcnow(),
        )
        session.add(account)
        user = User(
            id=str(uuid.uuid4()),
            account_id=account.id,
            trakt_username=trakt_username,
            trakt_user_id=trakt_user_id,
            display_name=trakt_username or None,
        )
        session.add(user)
        account.primary_user_id = user.id
    else:
        # Returning Trakt user. Ensure they have an Account (backfill for any
        # legacy row that escaped the startup migration).
        if not user.account_id:
            account = Account(
                id=str(uuid.uuid4()),
                primary_user_id=user.id,
                display_name=user.trakt_username or trakt_username,
                last_seen=datetime.utcnow(),
            )
            session.add(account)
            user.account_id = account.id
        else:
            account = await session.get(Account, user.account_id)
            if account is None:
                # FK integrity lost — rebuild it.
                account = Account(
                    id=user.account_id,
                    primary_user_id=user.id,
                    display_name=user.trakt_username or trakt_username,
                    last_seen=datetime.utcnow(),
                )
                session.add(account)
            else:
                account.last_seen = datetime.utcnow()

    user.trakt_access_token_enc = encrypt(access_token)
    user.trakt_refresh_token_enc = encrypt(refresh_token)
    user.trakt_token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    user.last_seen = datetime.utcnow()

    if is_new:
        # Create the three managed lists
        try:
            rec_movies = await trakt.create_list(
                access_token,
                "Reclio • Recommended Movies",
                "Auto-updated by Reclio with movies you'll love.",
            )
            rec_shows = await trakt.create_list(
                access_token,
                "Reclio • Recommended Shows",
                "Auto-updated by Reclio with shows you'll love.",
            )
            watch_progress = await trakt.create_list(
                access_token,
                "Reclio • Watch Progress",
                "Mirrors your Trakt playback progress for Chillio.",
            )
            user.trakt_rec_movies_list_id = ((rec_movies or {}).get("ids") or {}).get("trakt")
            user.trakt_rec_shows_list_id = ((rec_shows or {}).get("ids") or {}).get("trakt")
            user.trakt_watchprogress_list_id = ((watch_progress or {}).get("ids") or {}).get("trakt")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Creating managed lists failed: %s", exc)

        # Locate the built-in watchlist (optional — Trakt uses a virtual list)
        try:
            lists = await trakt.get_user_lists(access_token)
            for lst in lists:
                if (lst.get("name") or "").lower() == "watchlist":
                    user.trakt_watchlist_id = ((lst.get("ids") or {}).get("trakt"))
                    break
        except Exception:  # noqa: BLE001
            pass

    await session.commit()

    # Kick off the initial sync (non-blocking)
    try:
        from app.jobs.user_sync import sync_one_user

        asyncio.create_task(sync_one_user(user.id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to schedule initial sync: %s", exc)

    # Redirect to dashboard with a signed session cookie (signs Account.id).
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.delete_cookie(_STATE_COOKIE)
    response.delete_cookie("reclio_user")  # sunset legacy unsigned cookie
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(account.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    # Remember which Member is active — starts as the freshly-authed User.
    response.set_cookie(
        ACTIVE_MEMBER_COOKIE,
        create_active_member_token(user.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=ACTIVE_MEMBER_MAX_AGE,
    )
    return response


def _genre_pills(scores: dict | None, media_type: str, limit: int = 5) -> list[dict]:
    if not scores:
        return []
    table = MOVIE_GENRES if media_type == "movies" else TV_GENRES
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out = []
    for gid, score in ordered[:limit]:
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        name = table.get(gid_int)
        if not name:
            continue
        out.append({"name": name, "score": score})
    return out


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    settings = get_settings()
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=302)

    account = await session.get(Account, account_id)
    if account is None:
        response = RedirectResponse(url="/", status_code=302)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(ACTIVE_MEMBER_COOKIE)
        return response

    user = await _resolve_active_user(request, account, session)
    if user is None:
        # Account exists but has no Member — orphaned login. Force re-auth.
        response = RedirectResponse(url="/auth/trakt", status_code=302)
        return response

    account.last_seen = datetime.utcnow()
    await session.commit()

    taste = await session.get(TasteCache, user.id)

    addon_url = f"{settings.base_url.rstrip('/')}/?user_id={user.id}"

    # Populate the member switcher list for the header UI (Phase 9 hooks).
    members_result = await session.execute(
        select(User).where(User.account_id == account.id)
    )
    members = members_result.scalars().all()

    ctx = {
        "request": request,
        "settings": settings,
        "account": account,
        "user": user,
        "members": members,
        "taste": taste,
        "addon_url": addon_url,
        "movie_genres": _genre_pills(taste.movie_genre_scores if taste else None, "movies") if taste else [],
        "show_genres": _genre_pills(taste.show_genre_scores if taste else None, "shows") if taste else [],
        "top_actors": (taste.top_actors if taste else None) or [],
        "preferred_decade": (taste.preferred_decade if taste else None),
        "total_movies": (taste.total_movies_watched if taste else 0),
        "total_shows": (taste.total_shows_watched if taste else 0),
    }
    return templates.TemplateResponse("dashboard.html", ctx)


@router.post("/dashboard/refresh")
async def dashboard_refresh(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=303)

    account = await session.get(Account, account_id)
    if account is None:
        return RedirectResponse(url="/", status_code=303)

    user = await _resolve_active_user(request, account, session)
    if user is None:
        return RedirectResponse(url="/", status_code=303)

    taste = await session.get(TasteCache, user.id)
    if taste is not None:
        taste.is_stale = True
        await session.commit()

    try:
        from app.jobs.user_sync import sync_one_user

        asyncio.create_task(sync_one_user(user.id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Manual refresh failed to schedule: %s", exc)

    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/switch-member")
async def dashboard_switch_member(
    request: Request,
    member_id: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Switch the 'viewing as' Member within the same Account.

    Rejects attempts to switch to a member not owned by the current Account.
    """
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=303)

    member = await session.get(User, member_id)
    if member is None or member.account_id != account_id:
        raise HTTPException(status_code=404, detail="member not found")

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        ACTIVE_MEMBER_COOKIE,
        create_active_member_token(member.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=ACTIVE_MEMBER_MAX_AGE,
    )
    return response


@router.get("/signout")
@router.get("/logout")
async def signout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(ACTIVE_MEMBER_COOKIE)
    response.delete_cookie("reclio_user")  # sweep any legacy cookie
    return response
