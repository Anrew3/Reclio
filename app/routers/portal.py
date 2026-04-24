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
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.services.tmdb import MOVIE_GENRES, TV_GENRES
from app.services.trakt import get_trakt
from app.utils.crypto import encrypt

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["portal"])

_STATE_COOKIE = "reclio_oauth_state"


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
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

    # Find existing user by username (if any)
    user: User | None = None
    if trakt_username:
        result = await session.execute(
            select(User).where(User.trakt_username == trakt_username)
        )
        user = result.scalar_one_or_none()

    is_new = user is None
    if user is None:
        user = User(id=str(uuid.uuid4()), trakt_username=trakt_username, trakt_user_id=trakt_user_id)
        session.add(user)

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

    # Redirect to dashboard with a signed cookie identifying the user
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.delete_cookie(_STATE_COOKIE)
    response.set_cookie(
        "reclio_user",
        user.id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
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
    user_id = request.cookies.get("reclio_user")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/", status_code=302)

    taste = await session.get(TasteCache, user_id)

    addon_url = f"{settings.base_url.rstrip('/')}/?user_id={user.id}"

    ctx = {
        "request": request,
        "settings": settings,
        "user": user,
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
    user_id = request.cookies.get("reclio_user")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)

    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/", status_code=303)

    taste = await session.get(TasteCache, user_id)
    if taste is not None:
        taste.is_stale = True
        await session.commit()

    try:
        from app.jobs.user_sync import sync_one_user

        asyncio.create_task(sync_one_user(user.id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Manual refresh failed to schedule: %s", exc)

    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("reclio_user")
    return response
