"""Ask Reclio — read-only LLM chat about a viewer's recommendations.

Two endpoints:
  GET  /ask        renders the chat UI
  POST /ask/reply  returns a JSON {answer, error} for an XHR submission

Read-only: the assistant never triggers sync, mutates preferences, or
hits Trakt/Recombee write endpoints. It just synthesizes an answer from
the user's existing TasteCache + recent watch history.

Rate limit: a per-user token bucket (5 questions per 60 s). Cheap to
implement in-process — there's no horizontal scale model for this app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.account import Account
from app.models.taste_cache import TasteCache
from app.routers.portal import _current_account_id, _recently_watched, _resolve_active_user
from app.services.llm import get_llm
from app.services.tmdb import MOVIE_GENRES, TV_GENRES

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["ask"])

# --- Per-user rate limiter -----------------------------------------
# Keyed on User.id. Each entry is a deque of timestamps (monotonic).
# Trim on every check; old entries fall off naturally.

_RATE_WINDOW_SEC = 60.0
_RATE_MAX_HITS = 5

_rate_log: dict[str, Deque[float]] = {}
_rate_guard = asyncio.Lock()


async def _check_rate(user_id: str) -> bool:
    """Return True if the user is allowed another request right now."""
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SEC
    async with _rate_guard:
        log = _rate_log.setdefault(user_id, deque(maxlen=_RATE_MAX_HITS * 2))
        while log and log[0] < cutoff:
            log.popleft()
        if len(log) >= _RATE_MAX_HITS:
            return False
        log.append(now)
        return True


def _format_taste(taste: TasteCache | None) -> dict:
    """Turn TasteCache columns into the dict shape ask_reclio expects."""
    if taste is None:
        return {}

    def _names(scores: dict | None, table: dict[int, str]) -> list[str]:
        if not scores:
            return []
        ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out: list[str] = []
        for gid, _ in ordered:
            try:
                gid_int = int(gid)
            except (TypeError, ValueError):
                continue
            name = table.get(gid_int)
            if name:
                out.append(name)
        return out

    return {
        "top_movie_genres": _names(taste.movie_genre_scores, MOVIE_GENRES),
        "top_show_genres": _names(taste.show_genre_scores, TV_GENRES),
        "top_actors": [a.get("name") for a in (taste.top_actors or []) if a.get("name")],
        "preferred_decade": taste.preferred_decade,
    }


@router.get("/ask", response_class=HTMLResponse, response_model=None)
async def ask_form(
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

    llm = get_llm()
    return templates.TemplateResponse(
        "ask.html",
        {
            "request": request,
            "settings": settings,
            "user": user,
            "llm_enabled": llm.enabled,
            "llm_provider": llm.name,
        },
    )


@router.post("/ask/reply")
async def ask_reply(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """XHR endpoint: posts {question}, returns {answer} or {error}.

    Always 200 — errors are part of the JSON body so the client can render
    them inline without distinguishing transport vs business failures.
    """
    account_id = _current_account_id(request)
    if not account_id:
        return JSONResponse({"error": "Not signed in."}, status_code=200)

    account = await session.get(Account, account_id)
    if account is None:
        return JSONResponse({"error": "Session expired."}, status_code=200)

    user = await _resolve_active_user(request, account, session)
    if user is None:
        return JSONResponse({"error": "No member found."}, status_code=200)

    body = await request.json() if request.headers.get("content-type", "").startswith(
        "application/json"
    ) else None
    if body is None:
        try:
            form = await request.form()
            body = {"question": form.get("question")}
        except Exception:  # noqa: BLE001
            body = {}

    question = (body or {}).get("question") or ""
    question = question.strip()
    if not question:
        return JSONResponse({"error": "Type a question first."}, status_code=200)
    if len(question) > 500:
        return JSONResponse(
            {"error": "Question too long (500 character max)."}, status_code=200
        )

    if not await _check_rate(user.id):
        return JSONResponse(
            {"error": "Slow down — try again in a minute."}, status_code=200
        )

    llm = get_llm()
    if not llm.enabled:
        return JSONResponse(
            {
                "error": (
                    "Chat is offline — Reclio is running with no LLM provider. "
                    "Set LLM_PROVIDER in .env to enable."
                )
            },
            status_code=200,
        )

    taste = await session.get(TasteCache, user.id)

    # Recent watches double as concrete grounding for the model.
    try:
        recent = await _recently_watched(user, limit=8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ask: recently_watched failed for %s: %s", user.id, exc)
        recent = []

    try:
        answer = await llm.ask_reclio(
            question,
            user_taste=_format_taste(taste),
            recently_watched=recent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ask: LLM call failed for %s: %s", user.id, exc)
        answer = None

    if not answer:
        return JSONResponse(
            {"error": "Reclio couldn't come up with an answer right now."},
            status_code=200,
        )

    return JSONResponse({"answer": answer})
