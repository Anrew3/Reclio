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
from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.routers.onboarding import MOODS as MOOD_PALETTE
from app.routers.portal import _current_account_id, _recently_watched, _resolve_active_user
from app.services.llm import get_llm
from app.services.recombee import get_recombee
from app.services.tmdb import MOVIE_GENRES, TV_GENRES, get_tmdb

_TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w185"

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

    # Step 1: classify intent. Did the user ask to *change* something
    # ("stop showing me horror") or just *ask* ("why is this row here")?
    intent = "general"
    answer = None
    mutations: dict = {}
    applied_changes: list[str] = []

    try:
        classified = await llm.classify_chat_intent(
            question,
            mood_palette=MOOD_PALETTE,
            movie_genres=MOVIE_GENRES,
            tv_genres=TV_GENRES,
            recently_watched=recent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ask: intent classify failed for %s: %s", user.id, exc)
        classified = None

    if classified:
        intent = classified.get("intent", "general")
        answer = classified.get("answer")
        mutations = classified.get("mutations") or {}
        dislike = classified.get("dislike") or {}

        # ---- dislike_request: resolve title via TMDB and return a
        #      "pending" payload. The client renders a poster card with
        #      a "what didn't you like?" input that posts to
        #      /ask/dislike-confirm with the tmdb_id we resolved here.
        if intent == "dislike_request" and dislike.get("title"):
            tmdb = get_tmdb()
            kind = dislike.get("kind") or "movie"
            try:
                results = (
                    await tmdb.search_movie(dislike["title"], limit=1)
                    if kind == "movie"
                    else await tmdb.search_tv(dislike["title"], limit=1)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ask: TMDB search failed for %s: %s", dislike, exc)
                results = []

            if not results:
                return JSONResponse({
                    "answer": (
                        f"Couldn't find '{dislike['title']}' on TMDB. "
                        "Try the full title?"
                    ),
                    "intent": "general",
                })

            r = results[0]
            poster = r.get("poster_path")
            return JSONResponse({
                "answer": answer or "Got it — is this the one?",
                "intent": "dislike_pending",
                "pending": {
                    "tmdb_id": r.get("id"),
                    "kind": kind,
                    "title": r.get("title") if kind == "movie" else r.get("name"),
                    "year": (
                        (r.get("release_date") or "")[:4]
                        if kind == "movie"
                        else (r.get("first_air_date") or "")[:4]
                    ) or None,
                    "poster_url": f"{_TMDB_POSTER_BASE}{poster}" if poster else None,
                    "overview": (r.get("overview") or "")[:200],
                },
            })

    # Step 2: apply mutations if any. Each branch is best-effort and
    # never raises — chat survives DB hiccups.
    if intent == "mutate" and mutations:
        try:
            prefs = await session.get(UserPreferences, user.id)
            if prefs is None:
                prefs = UserPreferences(user_id=user.id)
                session.add(prefs)

            def _bump(field: str, delta_key: str) -> None:
                d = mutations.get(delta_key, 0)
                if not d:
                    return
                cur = getattr(prefs, field) or 50
                new = max(0, min(100, cur + d))
                if new != cur:
                    setattr(prefs, field, new)
                    direction = "up" if new > cur else "down"
                    applied_changes.append(f"{field.replace('_', ' ')} {direction}")

            _bump("era_preference", "delta_era")
            _bump("pacing_preference", "delta_pacing")
            _bump("runtime_preference", "delta_runtime")
            _bump("discovery_level", "delta_discovery")

            # Genre exclusions — set-union with existing
            for src_key, dst_attr in (
                ("exclude_movie_genres", "excluded_movie_genres"),
                ("exclude_show_genres", "excluded_show_genres"),
            ):
                add = mutations.get(src_key) or []
                if not add:
                    continue
                cur = list(getattr(prefs, dst_attr) or [])
                merged = sorted(set(cur) | set(add))
                if merged != cur:
                    setattr(prefs, dst_attr, merged)
                    applied_changes.append(f"{len(add)} genre(s) excluded")

            # Keyword lists — set-union, capped at 25 entries to stop
            # the prefs row growing unboundedly through chat.
            def _merge_kw(attr: str, add: list[str], label: str) -> None:
                if not add:
                    return
                cur = list(getattr(prefs, attr) or [])
                merged = []
                seen = set()
                for v in cur + add:
                    s = (v or "").strip().lower()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                merged = merged[:25]
                if merged != cur:
                    setattr(prefs, attr, merged)
                    applied_changes.append(label)

            _merge_kw("boosted_keywords", mutations.get("boost_keywords") or [], "boosted")
            _merge_kw("excluded_keywords", mutations.get("exclude_keywords") or [], "muted")

            # Title blocks — push as Recombee negative interactions for
            # any titles we can resolve to a TMDB id from recent watches
            # or the catalog. Even without resolution we keep the title
            # string for display so the user can see Reclio remembers.
            new_blocks = mutations.get("block_titles") or []
            if new_blocks:
                cur = list(prefs.blocked_titles or [])
                seen_titles = {b.get("title", "").lower() for b in cur}
                added = 0
                for b in new_blocks:
                    if b.get("title", "").lower() in seen_titles:
                        continue
                    cur.append(b)
                    added += 1
                if added:
                    prefs.blocked_titles = cur[:50]  # hard cap
                    applied_changes.append(f"{added} title(s) blocked")

                # Send Recombee a -1 rating for any block whose title
                # matches a recent watch — best-effort id resolution.
                recombee = get_recombee()
                if recombee.available:
                    title_to_item = {}
                    for w in recent:
                        if w.get("tmdb_id") and w.get("title"):
                            prefix = "movie" if w.get("kind") == "movie" else "tv"
                            title_to_item[w["title"].lower()] = f"{prefix}_{w['tmdb_id']}"
                    for b in new_blocks:
                        item_id = title_to_item.get(b.get("title", "").lower())
                        if item_id:
                            try:
                                await recombee.add_negative_interaction(user.id, item_id)
                            except Exception:  # noqa: BLE001
                                pass

            # Mark TasteCache stale so the next sync rebuilds rec lists
            # with the new excluded/boosted set.
            if applied_changes:
                cache = await session.get(TasteCache, user.id)
                if cache is not None:
                    cache.is_stale = True
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ask: mutation apply failed for %s: %s", user.id, exc)

    # Step 3: generate the natural reply if the classifier didn't already
    # return one (or returned an empty/missing answer for general/explain).
    if not answer and intent in ("explain", "general"):
        try:
            answer = await llm.ask_reclio(
                question,
                user_taste=_format_taste(taste),
                recently_watched=recent,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ask: LLM call failed for %s: %s", user.id, exc)

    if not answer:
        return JSONResponse(
            {"error": "Reclio couldn't come up with an answer right now."},
            status_code=200,
        )

    payload = {"answer": answer, "intent": intent}
    if applied_changes:
        payload["applied"] = applied_changes
    return JSONResponse(payload)


@router.post("/ask/dislike-confirm")
async def ask_dislike_confirm(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Step 2 of the interactive dislike flow.

    Body: {tmdb_id, kind, title, reason?, year?}
    Adds the title to UserPreferences.blocked_titles (with the
    optional reason note), pushes a Recombee -1 rating so collaborative
    filtering down-weights similar items, and replies with a short
    confirmation. Always returns HTTP 200; errors live in the body.
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

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    tmdb_id = body.get("tmdb_id")
    kind = body.get("kind")
    title = (body.get("title") or "").strip()
    reason = (body.get("reason") or "").strip()[:300]
    year = (body.get("year") or "").strip()[:4] if body.get("year") else None

    if not tmdb_id or kind not in ("movie", "tv") or not title:
        return JSONResponse(
            {"error": "Missing or invalid title selection."}, status_code=200
        )

    # Persist into blocked_titles. Each entry: {kind, tmdb_id, title, reason?, year?}.
    # Hard cap at 50 to keep the JSON column bounded.
    prefs = await session.get(UserPreferences, user.id)
    if prefs is None:
        prefs = UserPreferences(user_id=user.id)
        session.add(prefs)
    cur = list(prefs.blocked_titles or [])
    # Replace any existing entry for the same id+kind so we keep the
    # latest reason rather than ballooning duplicates.
    cur = [e for e in cur if not (e.get("tmdb_id") == tmdb_id and e.get("kind") == kind)]
    entry = {
        "kind": kind,
        "tmdb_id": int(tmdb_id),
        "title": title[:120],
    }
    if reason:
        entry["reason"] = reason
    if year:
        entry["year"] = year
    cur.append(entry)
    prefs.blocked_titles = cur[:50]

    # Mark TasteCache stale so the next sync rebuilds rec lists with
    # the new dislike applied.
    cache = await session.get(TasteCache, user.id)
    if cache is not None:
        cache.is_stale = True

    await session.commit()

    # Push Recombee a strong negative signal — RecommendItemsToUser /
    # RecommendItemsToItem both filter on this and propagate the dislike
    # to similar items in the latent space.
    item_id = f"{'movie' if kind == 'movie' else 'tv'}_{tmdb_id}"
    try:
        recombee = get_recombee()
        if recombee.available:
            await recombee.add_negative_interaction(user.id, item_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ask: recombee negative interaction failed: %s", exc)

    answer = (
        f"Got it — \"{title}\" is off your recommendations."
        + (f" Noted: {reason[:120]}" if reason else "")
    )
    return JSONResponse({
        "answer": answer,
        "intent": "dislike_confirmed",
        "applied": [f"blocked: {title}"],
    })
