"""Conversational onboarding — open-ended questions, LLM-derived profile.

Flow:
  - GET /onboarding (alias /preferences) renders five fun questions.
  - POST writes raw answers, asks the LLM to extract a structured
    profile, persists, sets onboarding_completed=True, redirects.
  - First-time Trakt users are bounced here from /dashboard once.
  - Returning users see the same form pre-filled with their answers
    so they can refine.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.account import Account
from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.routers.portal import _current_account_id, _resolve_active_user
from app.services.llm import get_llm
from app.services.tmdb import MOVIE_GENRES, TV_GENRES

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["onboarding"])

# Mood palette — kept short so the LLM has a constrained vocabulary
# when extracting tags from free-form answers.
MOODS = [
    "comfort", "intense", "uplifting", "thoughtful",
    "weird", "funny", "scary", "epic",
]

# The five conversational questions. Order matters — the LLM gets these
# Q/A pairs concatenated, so think about how each builds on the prior.
QUESTIONS = [
    {
        "key": "defining_pick",
        "label": "Pick a movie or show that defines your taste.",
        "placeholder": "e.g. Inception, The Bear, Spirited Away…",
        "max_len": 200,
    },
    {
        "key": "guilty_pleasure",
        "label": "What's your guilty pleasure?",
        "placeholder": "The thing you'd rewatch when no one's looking.",
        "max_len": 200,
    },
    {
        "key": "friday_mood",
        "label": "What kind of mood do you usually want from a Friday-night watch?",
        "placeholder": "Cozy, intense, weird, funny… use your own words.",
        "max_len": 300,
    },
    {
        "key": "era_take",
        "label": "Which era do you feel most at home in?",
        "placeholder": "70s noir? 90s sci-fi? Anything new and shiny?",
        "max_len": 200,
    },
    {
        "key": "no_thanks",
        "label": "Anything you never want to see?",
        "placeholder": "Genres, themes, anything off the table.",
        "max_len": 300,
    },
]


async def _get_or_create_prefs(session: AsyncSession, user_id: str) -> UserPreferences:
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
    answers = prefs.onboarding_answers or {}

    llm = get_llm()
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "settings": settings,
            "user": user,
            "prefs": prefs,
            "questions": [
                {**q, "value": answers.get(q["key"], "")} for q in QUESTIONS
            ],
            "llm_enabled": llm.enabled,
            "is_first_run": not prefs.onboarding_completed,
            "vibe_summary": prefs.vibe_summary,
        },
    )


@router.post("/onboarding")
@router.post("/preferences")
async def onboarding_save(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Persist raw answers, ask the LLM to derive a profile, save, redirect."""
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

    # Collect & length-cap raw answers. Empty strings are kept out so a
    # later re-derivation gets a clean signal of which prompts the user
    # actually engaged with.
    answers: dict[str, str] = {}
    for q in QUESTIONS:
        raw = (form.get(q["key"]) or "").strip()
        if raw:
            answers[q["key"]] = raw[: q["max_len"]]

    family_safe_explicit = form.get("family_safe") == "on"

    prefs = await _get_or_create_prefs(session, user.id)
    prefs.onboarding_answers = answers
    prefs.onboarding_completed = True
    prefs.updated_at = datetime.utcnow()

    # If the LLM is configured, ask it to derive structured prefs.
    # Otherwise we keep whatever values prefs already has — the family-safe
    # toggle still applies because the user set it explicitly.
    derived = None
    if answers:
        try:
            llm = get_llm()
            q_to_label = {q["key"]: q["label"] for q in QUESTIONS}
            labeled = {q_to_label[k]: v for k, v in answers.items()}
            derived = await llm.derive_preferences(
                labeled,
                mood_palette=MOODS,
                movie_genres=MOVIE_GENRES,
                tv_genres=TV_GENRES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("onboarding: LLM derivation failed for %s: %s", user.id, exc)

    if derived:
        prefs.favorite_moods = derived.get("favorite_moods") or []
        prefs.excluded_movie_genres = derived.get("excluded_movie_genres") or []
        prefs.excluded_show_genres = derived.get("excluded_show_genres") or []
        prefs.era_preference = derived.get("era_preference", 50)
        # The LLM-inferred family_safe is OR-ed with the explicit toggle so
        # checking the box always wins, but the LLM can also infer it from
        # answers like "I'm setting this up for my kids".
        prefs.family_safe = bool(derived.get("family_safe")) or family_safe_explicit
        prefs.vibe_summary = derived.get("vibe_summary")
    else:
        prefs.family_safe = family_safe_explicit

    # Mark TasteCache stale so the next sync rebuilds rec lists with the
    # new excluded-genre set.
    taste = await session.get(TasteCache, user.id)
    if taste is not None:
        taste.is_stale = True

    await session.commit()

    return RedirectResponse(url="/dashboard", status_code=303)
