"""Onboarding questionnaire — fires once per Member after the first
sync completes, then becomes the user-editable Preferences page.

Flow:
  - New Trakt user → first /dashboard hit redirects to /onboarding
    (only when prefs.onboarding_completed is False).
  - Returning user → /preferences (alias) renders the same form
    pre-filled with current values.
  - POST /onboarding writes the row, sets onboarding_completed=True,
    invalidates the user's TasteCache so the next sync picks up the
    new preferences via feed_builder, then redirects to /dashboard.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.account import Account
from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.routers.portal import _current_account_id, _resolve_active_user
from app.services.tmdb import MOVIE_GENRES, TV_GENRES

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["onboarding"])

# Fixed mood palette. Kept short on purpose — too many options = decision
# fatigue and noisy data. These map loosely to the Recombee context filters
# but for now just inform UI copy and feed-builder weights.
MOODS = [
    {"key": "comfort",   "label": "Cozy & Comforting"},
    {"key": "intense",   "label": "Edge-of-seat Intense"},
    {"key": "uplifting", "label": "Feel-good Uplifting"},
    {"key": "thoughtful","label": "Slow & Thoughtful"},
    {"key": "weird",     "label": "Weird & Surreal"},
    {"key": "funny",     "label": "Laugh-out-loud Funny"},
    {"key": "scary",     "label": "Spooky & Scary"},
    {"key": "epic",      "label": "Sweeping & Epic"},
]


def _parse_int(raw: str | None, default: int, lo: int = 0, hi: int = 100) -> int:
    """Clamp slider input to [lo, hi]; never raise."""
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _parse_int_list(values: Iterable[str] | None) -> list[int]:
    """Multi-select form values come as repeated strings; strip + dedupe."""
    if not values:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for v in values:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in seen:
            seen.add(iv)
            out.append(iv)
    return out


def _parse_str_list(values: Iterable[str] | None, allowed: set[str]) -> list[str]:
    """Whitelist string values against a fixed palette."""
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v in allowed and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _get_or_create_prefs(session: AsyncSession, user_id: str) -> UserPreferences:
    """Load preferences row, creating a default if missing."""
    prefs = await session.get(UserPreferences, user_id)
    if prefs is None:
        prefs = UserPreferences(user_id=user_id)
        session.add(prefs)
    return prefs


@router.get("/onboarding", response_class=HTMLResponse, response_model=None)
@router.get("/preferences", response_class=HTMLResponse, response_model=None)
async def onboarding_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=302)

    account = await session.get(Account, account_id)
    if account is None:
        return RedirectResponse(url="/", status_code=302)

    user = await _resolve_active_user(request, account, session)
    if user is None:
        return RedirectResponse(url="/auth/trakt", status_code=302)

    prefs = await _get_or_create_prefs(session, user.id)

    ctx = {
        "request": request,
        "settings": settings,
        "user": user,
        "prefs": prefs,
        "movie_genres": sorted(MOVIE_GENRES.items(), key=lambda x: x[1]),
        "show_genres": sorted(TV_GENRES.items(), key=lambda x: x[1]),
        "moods": MOODS,
        "is_first_run": not prefs.onboarding_completed,
    }
    return templates.TemplateResponse("onboarding.html", ctx)


@router.post("/onboarding")
@router.post("/preferences")
async def onboarding_save(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Persist the questionnaire. Tolerant of partial input — anything
    missing falls back to its current/default value."""
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=303)

    account = await session.get(Account, account_id)
    if account is None:
        return RedirectResponse(url="/", status_code=303)

    user = await _resolve_active_user(request, account, session)
    if user is None:
        return RedirectResponse(url="/", status_code=303)

    form = await request.form()

    prefs = await _get_or_create_prefs(session, user.id)
    prefs.discovery_level = _parse_int(form.get("discovery_level"), default=prefs.discovery_level)
    prefs.era_preference = _parse_int(form.get("era_preference"), default=prefs.era_preference)
    prefs.excluded_movie_genres = _parse_int_list(form.getlist("excluded_movie_genres"))
    prefs.excluded_show_genres = _parse_int_list(form.getlist("excluded_show_genres"))
    allowed_moods = {m["key"] for m in MOODS}
    prefs.favorite_moods = _parse_str_list(form.getlist("favorite_moods"), allowed_moods)
    prefs.family_safe = form.get("family_safe") == "on"
    prefs.onboarding_completed = True
    prefs.updated_at = datetime.utcnow()

    # Mark TasteCache stale so the next sync rebuilds rec lists. The
    # taste profile itself is unaffected — but the feed_builder weighs
    # preferences on every request, so flagging stale isn't strictly
    # required. We do it anyway so a user who tweaks preferences sees a
    # fresh "Last synced" timestamp on the dashboard.
    taste = await session.get(TasteCache, user.id)
    if taste is not None:
        taste.is_stale = True

    await session.commit()

    return RedirectResponse(url="/dashboard", status_code=303)
