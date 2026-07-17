"""The /recommendations page: browse everything the engine is
recommending, and talk back.

GET  /recommendations           full grid (movies + shows), live from
                                the engine — always current, no waiting
                                for the next Trakt-list sync.
POST /recommendations/feedback  {item_id, comment | quick} — records the
                                reaction, updates the taste profile, and
                                kicks a background re-sync so the Chillio
                                rows follow.

How a comment changes recommendations (three channels at once):
  1. The LLM parses it into structured signal — sentiment, aspect
     phrases, keyword boosts/mutes, genre exclusions, hard blocks —
     which lands in the interaction store + preferences.
  2. The comment text itself is embedded (same vector space as the
     catalog) and joins the taste profile with the sentiment's sign:
     "loved the slow-burn tension" literally pulls future picks toward
     slow-burn tension.
  3. A background sync refreshes the managed Trakt lists so Chillio
     sees the updated row within seconds.
With no LLM configured, a small sentiment lexicon keeps channels 2-3
working — feedback never lands on the floor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session, session_scope
from app.models.account import Account
from app.models.content import ContentCatalog
from app.models.feedback import RecFeedback
from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.routers.portal import _current_account_id, _resolve_active_user
from app.services import recommender
from app.services.llm import get_llm
from app.services.tmdb import MOVIE_GENRES, TV_GENRES, get_tmdb

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["recommendations"])

_POSTER_BASE = "https://image.tmdb.org/t/p/w342"
_GRID_COUNT = 24  # per media type

# --- Per-user rate limiter (feedback writes) ------------------------
_RATE_WINDOW_SEC = 60.0
_RATE_MAX_HITS = 10
_rate_log: dict[str, Deque[float]] = {}
_rate_guard = asyncio.Lock()


async def _check_rate(user_id: str) -> bool:
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


# --- Sentiment lexicon fallback (LLM offline) ------------------------

_POS_WORDS = {
    "love", "loved", "loving", "great", "amazing", "awesome", "perfect",
    "favorite", "favourite", "fantastic", "brilliant", "beautiful",
    "excellent", "more", "yes", "like", "liked", "enjoyed", "fun",
    "masterpiece", "gripping", "cozy", "excited", "interested",
}
_NEG_WORDS = {
    "hate", "hated", "boring", "bored", "awful", "terrible", "bad",
    "worse", "worst", "no", "not", "never", "stop", "dislike", "disliked",
    "meh", "slow", "dull", "annoying", "skip", "gross", "creepy",
    "predictable", "overrated", "cheesy", "trash", "dumb",
}


def _lexicon_sentiment(comment: str) -> float:
    """Crude signed sentiment in [-0.7, 0.7] — enough to sign the
    comment embedding when no LLM is configured."""
    words = {w.strip(".,!?'\"").lower() for w in comment.split()}
    pos = len(words & _POS_WORDS)
    neg = len(words & _NEG_WORDS)
    if pos == neg:
        return 0.2 if pos else 0.0  # a neutral note still counts a little
    raw = (pos - neg) / max(1, pos + neg)
    return max(-0.7, min(0.7, round(raw * 0.7, 2)))


# --- Hydration -------------------------------------------------------


async def _hydrate(item_ids: list[str]) -> list[dict[str, Any]]:
    """Catalog rows → display dicts. Missing posters are fetched from
    TMDB once and written back to the catalog (self-healing)."""
    if not item_ids:
        return []
    async with session_scope() as session:
        result = await session.execute(
            select(ContentCatalog).where(ContentCatalog.tmdb_id.in_(item_ids))
        )
        rows = {r.tmdb_id: r for r in result.scalars().all()}

        # Backfill missing posters (bounded, cached client-side 6h).
        missing = [r for r in rows.values() if not r.poster_path][:12]
        if missing:
            tmdb = get_tmdb()

            async def _fetch(row: ContentCatalog) -> None:
                try:
                    raw_id = int(row.tmdb_id.split("_", 1)[1])
                    data = (
                        await tmdb.get_movie(raw_id)
                        if row.media_type == "movie"
                        else await tmdb.get_show(raw_id)
                    )
                    if data and data.get("poster_path"):
                        row.poster_path = data["poster_path"]
                except Exception as exc:  # noqa: BLE001
                    logger.debug("recs: poster backfill failed for %s: %s",
                                 row.tmdb_id, exc)

            await asyncio.gather(*[_fetch(r) for r in missing])

    out: list[dict[str, Any]] = []
    for item_id in item_ids:
        row = rows.get(item_id)
        if row is None:
            continue
        out.append({
            "item_id": item_id,
            "title": row.title,
            "year": row.year,
            "media_type": row.media_type,
            "genres": [g.get("name") for g in (row.genres or []) if g.get("name")][:3],
            "vote_average": round(row.vote_average, 1) if row.vote_average else None,
            "poster_url": f"{_POSTER_BASE}{row.poster_path}" if row.poster_path else None,
        })
    return out


# --- Page ------------------------------------------------------------


@router.get("/recommendations", response_class=HTMLResponse, response_model=None)
async def recommendations_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    account_id = _current_account_id(request)
    if not account_id:
        return RedirectResponse(url="/", status_code=302)
    account = await session.get(Account, account_id)
    if account is None:
        return RedirectResponse(url="/", status_code=302)
    user = await _resolve_active_user(request, account, session)
    if user is None:
        return RedirectResponse(url="/auth/trakt", status_code=302)

    movie_ids, show_ids = await asyncio.gather(
        recommender.get_recommendations(user.id, count=_GRID_COUNT, media_type="movie"),
        recommender.get_recommendations(user.id, count=_GRID_COUNT, media_type="tv"),
    )
    movies, shows = await asyncio.gather(
        _hydrate(movie_ids), _hydrate(show_ids),
    )

    # Attach the viewer's latest reaction per item so the UI can show
    # "noted" state across reloads.
    fb_result = await session.execute(
        select(RecFeedback)
        .where(RecFeedback.user_id == user.id)
        .order_by(RecFeedback.created_at.asc())
    )
    latest: dict[str, RecFeedback] = {}
    for fb in fb_result.scalars().all():
        latest[fb.item_id] = fb
    for entry in movies + shows:
        fb = latest.get(entry["item_id"])
        if fb is not None:
            entry["feedback"] = {
                "sentiment": fb.sentiment,
                "comment": (fb.comment or "")[:140] or None,
            }

    llm = get_llm()
    return templates.TemplateResponse(
        "recommendations.html",
        {
            "request": request,
            "user": user,
            "movies": movies,
            "shows": shows,
            "llm_enabled": llm.enabled,
        },
    )


# --- Feedback --------------------------------------------------------


def _merge_keywords(prefs: UserPreferences, attr: str, add: list[str]) -> bool:
    if not add:
        return False
    cur = list(getattr(prefs, attr) or [])
    merged, seen = [], set()
    for v in cur + add:
        s = (v or "").strip().lower()
        if s and s not in seen:
            seen.add(s)
            merged.append(s)
    merged = merged[:25]
    if merged != cur:
        setattr(prefs, attr, merged)
        return True
    return False


@router.post("/recommendations/feedback")
async def recommendations_feedback(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Record a reaction to a recommended title.

    Body: {"item_id": "movie_550", "comment": "..."} for written
    feedback, or {"item_id": ..., "quick": "up"|"down"} for one-tap
    reactions. Always HTTP 200; errors live in the JSON body.
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
    item_id = str(body.get("item_id") or "")
    comment = (body.get("comment") or "").strip()[:500]
    quick = body.get("quick") if body.get("quick") in ("up", "down") else None

    if not (item_id.startswith("movie_") or item_id.startswith("tv_")):
        return JSONResponse({"error": "Unknown title."}, status_code=200)
    if not comment and not quick:
        return JSONResponse({"error": "Say something first."}, status_code=200)
    if not await _check_rate(user.id):
        return JSONResponse({"error": "Slow down — try again in a minute."}, status_code=200)

    row = await session.get(ContentCatalog, item_id)
    title = row.title if row else item_id
    media_type = row.media_type if row else ("movie" if item_id.startswith("movie_") else "tv")
    genres = [g.get("name") for g in ((row.genres if row else None) or []) if g.get("name")]

    applied: list[str] = []
    parsed: dict[str, Any] | None = None
    sentiment: float
    reply: str | None = None

    if quick:
        sentiment = 0.8 if quick == "up" else -0.8
        parsed = {"source": "quick", "reaction": quick}
        applied.append("thumbs " + quick)
    else:
        # LLM parse with lexicon fallback — feedback always lands.
        try:
            parsed = await get_llm().parse_recommendation_feedback(
                comment,
                title=title,
                media_type=media_type,
                genres=genres,
                movie_genres=MOVIE_GENRES,
                tv_genres=TV_GENRES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recs: LLM feedback parse failed: %s", exc)
            parsed = None
        if parsed is None:
            sentiment = _lexicon_sentiment(comment)
            parsed = {"source": "heuristic", "sentiment": sentiment}
        else:
            sentiment = parsed["sentiment"]
            reply = parsed.get("reply")

    # 1. Interaction — the profile's strongest lever.
    try:
        await recommender.record_interactions([{
            "kind": "feedback", "user_id": user.id, "item_id": item_id,
            "weight": sentiment, "timestamp": datetime.utcnow(),
        }])
        applied.append(f"taste updated ({'+' if sentiment >= 0 else ''}{sentiment:.1f})")
    except Exception as exc:  # noqa: BLE001
        logger.warning("recs: interaction store failed: %s", exc)

    # 2. Embed the comment text into profile space.
    embedding_bytes = None
    embedding_dim = None
    if comment:
        try:
            from app.services.embeddings import embed_text
            vec = await embed_text(comment)
            if vec:
                from app.jobs.content_sync import _pack_embedding
                embedding_bytes = _pack_embedding(vec)
                embedding_dim = len(vec)
                applied.append("comment folded into your taste profile")
        except Exception as exc:  # noqa: BLE001
            logger.debug("recs: comment embed failed: %s", exc)

    # 3. Structured actions from the parse.
    prefs = await session.get(UserPreferences, user.id)
    if prefs is None:
        prefs = UserPreferences(user_id=user.id)
        session.add(prefs)

    if parsed.get("block"):
        try:
            await recommender.add_negative_interaction(user.id, item_id)
        except Exception:  # noqa: BLE001
            pass
        cur = list(prefs.blocked_titles or [])
        raw_id = int(item_id.split("_", 1)[1])
        cur = [e for e in cur if not (e.get("tmdb_id") == raw_id and e.get("kind") == media_type)]
        cur.append({"kind": media_type, "tmdb_id": raw_id, "title": title[:120]})
        prefs.blocked_titles = cur[:50]
        applied.append(f"blocked: {title}")

    if _merge_keywords(prefs, "boosted_keywords", parsed.get("boost_keywords") or []):
        applied.append("boosted: " + ", ".join(parsed["boost_keywords"]))
    if _merge_keywords(prefs, "excluded_keywords", parsed.get("exclude_keywords") or []):
        applied.append("muted: " + ", ".join(parsed["exclude_keywords"]))

    for src_key, dst_attr in (
        ("exclude_movie_genres", "excluded_movie_genres"),
        ("exclude_show_genres", "excluded_show_genres"),
    ):
        add = parsed.get(src_key) or []
        if add:
            cur = list(getattr(prefs, dst_attr) or [])
            merged = sorted(set(cur) | set(add))
            if merged != cur:
                setattr(prefs, dst_attr, merged)
                applied.append(f"{len(add)} genre(s) excluded")

    # 4. Persist the feedback record + mark the profile stale.
    session.add(RecFeedback(
        user_id=user.id,
        item_id=item_id,
        title=title[:200],
        comment=comment or None,
        sentiment=sentiment,
        parsed=parsed,
        embedding=embedding_bytes,
        embedding_dim=embedding_dim,
    ))
    cache = await session.get(TasteCache, user.id)
    if cache is not None:
        cache.is_stale = True
    await session.commit()

    # 5. Background re-sync so the Chillio rows follow within seconds.
    try:
        from app.jobs.user_sync import sync_one_user
        asyncio.create_task(sync_one_user(user.id, force=True))
    except Exception as exc:  # noqa: BLE001
        logger.debug("recs: background sync schedule failed: %s", exc)

    if not reply:
        if quick == "up":
            reply = f"Noted — more like “{title}” coming up."
        elif quick == "down":
            reply = f"Got it — steering away from “{title}”."
        elif sentiment >= 0.3:
            reply = "Love it — I'll lean into that."
        elif sentiment <= -0.3:
            reply = "Understood — less of that from now on."
        else:
            reply = "Noted — folded into your taste profile."

    return JSONResponse({
        "reply": reply,
        "sentiment": sentiment,
        "applied": applied,
    })
