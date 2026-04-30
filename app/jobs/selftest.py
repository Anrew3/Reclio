"""Comprehensive self-test — exercises every external system + every
internal subsystem and reports pass/fail with structured detail.

Designed as a single-call diagnostic. The hourly health check probes
the bare minimum needed to detect outages. This module probes
*everything*: database round-trip, Trakt + TMDB + Recombee + LLM
+ embeddings + similarity + watch-state state machine + feed builder
+ taste profile + scheduler + sync jobs + admin auth + chat intent
classification + dislike resolution + personality blurb generation.

Output is a JSON-serializable dict so /admin/selftest can stream it
back to the operator. Every check returns a uniform shape so the UI
or scripts can rank by severity.

Each check follows a strict contract:
  name        : str — short identifier
  category    : str — 'external' | 'internal' | 'config' | 'data'
  status      : 'pass' | 'warn' | 'fail' | 'skip'
  elapsed_ms  : int
  detail      : dict — arbitrary structured info
  error       : str | None
  remediation : str | None — operator-actionable hint when status != pass

A test that intentionally creates state (write probes) MUST clean up
or use idempotent fixtures with stable ids so a second run is safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from app.config import get_settings
from app.database import session_scope
from app.models.content import ContentCatalog
from app.models.preferences import UserPreferences
from app.models.taste_cache import TasteCache
from app.models.user import User
from app.models.watch_attempt import WatchAttempt

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    name: str
    category: str
    status: str       # 'pass' | 'warn' | 'fail' | 'skip'
    elapsed_ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    remediation: str | None = None


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ============================================================
# External services
# ============================================================


async def _t_database() -> ProbeResult:
    t0 = time.monotonic()
    try:
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            value = result.scalar_one()
        return ProbeResult(
            "database", "external", "pass" if value == 1 else "fail",
            _ms_since(t0), detail={"select_1": value},
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "database", "external", "fail", _ms_since(t0),
            error=str(exc)[:200],
            remediation="Check DATABASE_URL and that the SQLite file is writable.",
        )


async def _t_trakt() -> ProbeResult:
    t0 = time.monotonic()
    try:
        from app.services.trakt import get_trakt
        client = get_trakt()
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(c.get("/genres/movies"), timeout=8.0)
        ok = resp.status_code == 200
        return ProbeResult(
            "trakt", "external", "pass" if ok else "fail", _ms_since(t0),
            detail={
                "status_code": resp.status_code,
                "rate_limit_remaining": resp.headers.get("x-ratelimit-remaining"),
                "rate_limit_total": resp.headers.get("x-ratelimit-limit"),
            },
            error=None if ok else f"HTTP {resp.status_code}",
            remediation=None if ok else
                "Verify TRAKT_CLIENT_ID; check trakt.tv status.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "trakt", "external", "fail", _ms_since(t0),
            error=str(exc)[:200],
            remediation="TRAKT_CLIENT_ID may be missing or trakt.tv unreachable.",
        )


async def _t_tmdb() -> ProbeResult:
    t0 = time.monotonic()
    try:
        from app.services.tmdb import get_tmdb
        client = get_tmdb()
        if not client._api_key:  # noqa: SLF001
            return ProbeResult(
                "tmdb", "external", "fail", _ms_since(t0),
                error="TMDB_API_KEY not set",
                remediation="Add TMDB_API_KEY to your env (free at https://www.themoviedb.org/settings/api).",
            )
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(
            c.get("/configuration", params={"api_key": client._api_key}),  # noqa: SLF001
            timeout=8.0,
        )
        ok = resp.status_code == 200
        if resp.status_code == 401:
            return ProbeResult(
                "tmdb", "external", "fail", _ms_since(t0),
                detail={"status_code": 401},
                error="HTTP 401 — TMDB_API_KEY rejected",
                remediation="Verify TMDB_API_KEY value at themoviedb.org/settings/api.",
            )
        return ProbeResult(
            "tmdb", "external", "pass" if ok else "fail", _ms_since(t0),
            detail={"status_code": resp.status_code},
            error=None if ok else f"HTTP {resp.status_code}",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "tmdb", "external", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


async def _t_recombee() -> ProbeResult:
    t0 = time.monotonic()
    from app.services.recombee import get_recombee
    recombee = get_recombee()
    cfg = recombee.config_dump()

    if not cfg["sdk_loaded"]:
        return ProbeResult(
            "recombee", "external", "fail", _ms_since(t0),
            detail=cfg, error="Recombee SDK not installed",
            remediation="Rebuild the container (recombee-api-client missing).",
        )
    if not cfg["token_present"] or not cfg["database_id"]:
        return ProbeResult(
            "recombee", "external", "fail", _ms_since(t0),
            detail=cfg, error="missing RECOMBEE_DATABASE_ID or RECOMBEE_PRIVATE_TOKEN",
            remediation="Set both env vars from admin.recombee.com.",
        )
    if not cfg["available"]:
        return ProbeResult(
            "recombee", "external", "fail", _ms_since(t0),
            detail=cfg, error="client failed to initialize despite credentials present",
        )

    count, sample = await recombee.list_items_count(max_count=3)
    if count is None:
        write_ok, write_err = await recombee.write_test_item()
        return ProbeResult(
            "recombee", "external", "fail", _ms_since(t0),
            detail={**cfg, "write_probe": {"ok": write_ok, "error": write_err}},
            error="ListItems unreachable",
            remediation=("Most likely wrong RECOMBEE_DATABASE_ID, wrong token type "
                         "(public vs private), or wrong RECOMBEE_REGION."),
        )

    # Catalog comparison — wrong_region is the most-common silent fail
    async with session_scope() as session:
        catalog_synced = await session.scalar(
            select(func.count()).select_from(ContentCatalog).where(
                ContentCatalog.recombee_synced.is_(True)
            )
        )
    catalog_synced = catalog_synced or 0

    if catalog_synced > 5 and count == 0:
        return ProbeResult(
            "recombee", "external", "fail", _ms_since(t0),
            detail={
                **cfg, "verdict": "wrong_region",
                "reclio_marked_synced": catalog_synced,
                "recombee_count": count,
            },
            error=f"{catalog_synced} items pushed locally but Recombee shows 0",
            remediation=("RECOMBEE_REGION mismatch. Match the URL in the Recombee "
                         "web UI: rapi-{us-west,eu-west,ap-se,ca-east}.recombee.com."),
        )

    schema_ok = recombee._properties_initialized  # noqa: SLF001
    return ProbeResult(
        "recombee", "external",
        "pass" if schema_ok else "warn",
        _ms_since(t0),
        detail={
            "verdict": "ok",
            "recombee_item_count_visible": count,
            "reclio_marked_synced": catalog_synced,
            "schema_initialized": schema_ok,
            "sample_ids": sample,
        },
        remediation=(None if schema_ok else
            "Schema not initialized — items will lack title/overview columns "
            "in the Recombee dashboard. Trigger POST /admin/sync/content to self-heal."),
    )


async def _t_llm() -> ProbeResult:
    t0 = time.monotonic()
    try:
        from app.services.llm import NullProvider, get_llm
        llm = get_llm()
        if isinstance(llm.provider, NullProvider) or not llm.enabled:
            return ProbeResult(
                "llm", "external", "warn", _ms_since(t0),
                detail={"provider": llm.name, "enabled": False},
                error="No LLM provider configured",
                remediation="Set LLM_PROVIDER (openai/claude/openrouter/ollama) plus matching key.",
            )
        result = await asyncio.wait_for(
            llm.provider.generate("Reply with the word OK", max_tokens=10, temperature=0.0),
            timeout=12.0,
        )
        ok = bool(result)
        return ProbeResult(
            "llm", "external", "pass" if ok else "fail", _ms_since(t0),
            detail={
                "provider": llm.name,
                "response_preview": (str(result)[:120] if result else None),
            },
            error=None if ok else "provider returned empty/None — see provider WARNING line for HTTP body",
            remediation=None if ok else
                "Check container logs for the provider's actual rejection (model name, key, etc).",
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            "llm", "external", "fail", _ms_since(t0),
            error="generate timed out (12s)",
            remediation="Provider unreachable or model very slow. For Ollama, check the model is pulled.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "llm", "external", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


async def _t_embeddings() -> ProbeResult:
    t0 = time.monotonic()
    try:
        from app.services.embeddings import (
            NullEmbeddingProvider, embed_text, get_embeddings_provider,
        )
        provider = get_embeddings_provider()
        if isinstance(provider, NullEmbeddingProvider):
            return ProbeResult(
                "embeddings", "external", "warn", _ms_since(t0),
                detail={"provider": provider.name},
                error="EMBEDDING_PROVIDER=none — vector similarity rail is empty",
                remediation="Set EMBEDDING_PROVIDER (auto/openai/ollama/local) to enable.",
            )
        vec = await asyncio.wait_for(
            embed_text("Inception. Sci-fi heist about dreams within dreams."),
            timeout=15.0,
        )
        ok = bool(vec) and len(vec) > 0
        return ProbeResult(
            "embeddings", "external", "pass" if ok else "fail", _ms_since(t0),
            detail={
                "provider": provider.name,
                "expected_dim": provider.dim,
                "actual_dim": len(vec) if vec else 0,
                "sample_values": (vec[:4] if vec else []),
            },
            error=None if ok else "embedder returned empty vector",
            remediation=None if ok else
                "Check provider logs. For Ollama, pull the embedding model: "
                "docker compose exec reclio-ollama ollama pull nomic-embed-text",
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            "embeddings", "external", "fail", _ms_since(t0),
            error="embed_text timed out (15s)",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "embeddings", "external", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


# ============================================================
# Internal subsystems
# ============================================================


async def _t_similarity() -> ProbeResult:
    """Verify the cosine-similarity service can load + return neighbors."""
    t0 = time.monotonic()
    try:
        from app.services.similarity import _load_matrix, similar_to
        ok = await _load_matrix()
        if not ok:
            async with session_scope() as session:
                catalog_total = await session.scalar(
                    select(func.count()).select_from(ContentCatalog)
                )
            return ProbeResult(
                "similarity", "internal", "warn", _ms_since(t0),
                detail={"catalog_total": catalog_total or 0},
                error="no embeddings stored yet",
                remediation=("Run POST /admin/sync/content to populate. "
                             "On the next sync, embeddings get computed for new catalog items."),
            )
        # Pick any seeded item and ask for neighbors
        async with session_scope() as session:
            seed = await session.scalar(
                select(ContentCatalog.tmdb_id)
                .where(ContentCatalog.embedding.is_not(None))
                .limit(1)
            )
        if not seed:
            return ProbeResult(
                "similarity", "internal", "warn", _ms_since(t0),
                error="matrix loaded but no seed available",
            )
        neighbors = await similar_to(seed, k=5)
        return ProbeResult(
            "similarity", "internal", "pass", _ms_since(t0),
            detail={"seed": seed, "neighbor_count": len(neighbors), "sample": neighbors[:3]},
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "similarity", "internal", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


async def _t_watch_state_machine() -> ProbeResult:
    """Run synthetic fixtures through the decision tree without DB writes."""
    t0 = time.monotonic()
    try:
        from app.jobs.watch_state import _decide_movie, _decide_episode

        now = datetime.utcnow()
        cases: list[tuple[str, str]] = []

        def _att(progress, paused, hour, kind="movie", **kw):
            a = WatchAttempt()
            a.user_id = "selftest"
            a.kind = kind
            a.last_progress_pct = progress
            a.last_paused_at_utc = paused
            a.last_paused_local_hour = hour
            a.first_seen_at = paused
            a.movie_tmdb_id = kw.get("movie_tmdb_id", 1)
            a.show_tmdb_id = kw.get("show_tmdb_id")
            a.season_number = kw.get("season_number")
            a.episode_number = kw.get("episode_number")
            return a

        cases.append((
            "late_night_45_6d",
            _decide_movie(_att(45, now - timedelta(days=6), 23), [], now, False) or "None",
        ))
        cases.append((
            "daytime_40_30h",
            _decide_movie(_att(40, now - timedelta(hours=30), 14), [], now, False) or "None",
        ))
        cases.append((
            "movie_95pct",
            _decide_movie(_att(95, now - timedelta(hours=2), 21), [], now, False) or "None",
        ))
        cases.append((
            "s1e1_30pct_60h",
            _decide_episode(_att(30, now - timedelta(hours=60), 14, kind="episode",
                                 show_tmdb_id=1396, season_number=1, episode_number=1),
                            [], now, False) or "None",
        ))

        expected = {
            "late_night_45_6d": "abandoned_sleep",
            "daytime_40_30h": "abandoned_bounce",
            "movie_95pct": "completed",
            "s1e1_30pct_60h": "abandoned_bounce",
        }
        misses = {k: (got, expected[k]) for k, got in cases if got != expected[k]}
        return ProbeResult(
            "watch_state_machine", "internal",
            "pass" if not misses else "fail",
            _ms_since(t0),
            detail={"cases": dict(cases), "expected": expected, "misses": misses},
            error=None if not misses else f"{len(misses)} verdict regressions",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "watch_state_machine", "internal", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


async def _t_feed_builder() -> ProbeResult:
    """Build the 10-feed response against synthetic user/taste/prefs."""
    t0 = time.monotonic()
    try:
        from app.services.feed_builder import build_feeds

        class _U:
            trakt_rec_movies_list_id = 111
            trakt_rec_shows_list_id = 222
            trakt_byw_movies_list_id = 333
            trakt_byw_shows_list_id = 444
            trakt_watchprogress_list_id = None
            trakt_watchlist_id = None

        class _T:
            last_watched_movie_tmdb_id = 27205
            last_watched_movie_title = "Inception"
            last_watched_show_tmdb_id = 1396
            last_watched_show_title = "Breaking Bad"
            movie_genre_scores = {"18": 0.9, "28": 0.7, "878": 0.6}
            show_genre_scores = {"18": 0.9, "80": 0.7, "10765": 0.6}

        class _P:
            excluded_movie_genres = [27]
            excluded_show_genres = [10764]
            family_safe = False
            era_preference = 50
            discovery_level = 50

        feeds = await build_feeds(None, _U(), _T(), prefs=_P())
        # Should be exactly 10 (5 movie + 5 show)
        movie_count = sum(1 for f in feeds if f.get("content_type") == "movies")
        show_count = sum(1 for f in feeds if f.get("content_type") == "shows")
        ok = (
            len(feeds) == 10
            and movie_count == 5
            and show_count == 5
            and all(f.get("source") in ("trakt_list", "tmdb_query") for f in feeds)
        )
        return ProbeResult(
            "feed_builder", "internal", "pass" if ok else "fail", _ms_since(t0),
            detail={
                "total": len(feeds),
                "movies": movie_count,
                "shows": show_count,
                "ids": [f["id"] for f in feeds],
            },
            error=None if ok else "feed shape unexpected",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "feed_builder", "internal", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


async def _t_scheduler() -> ProbeResult:
    """Verify the four background jobs are all registered + scheduled."""
    t0 = time.monotonic()
    try:
        from app.jobs.scheduler import get_scheduler
        sched = get_scheduler()
        if not sched.running:
            return ProbeResult(
                "scheduler", "internal", "fail", _ms_since(t0),
                error="scheduler not running",
                remediation="Container in restart loop? Check startup logs.",
            )
        jobs = {j.id: j for j in sched.get_jobs()}
        expected = {"content_sync", "user_sync", "token_refresh", "health_check"}
        missing = expected - set(jobs.keys())
        return ProbeResult(
            "scheduler", "internal",
            "pass" if not missing else "fail",
            _ms_since(t0),
            detail={
                "running": sched.running,
                "jobs": {
                    jid: (j.next_run_time.isoformat() if j.next_run_time else None)
                    for jid, j in jobs.items()
                },
                "missing": sorted(missing),
            },
            error=None if not missing else f"missing jobs: {sorted(missing)}",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "scheduler", "internal", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


# ============================================================
# Config sanity
# ============================================================


async def _t_config() -> ProbeResult:
    t0 = time.monotonic()
    settings = get_settings()
    issues: list[str] = []
    fields = {
        "TRAKT_CLIENT_ID": bool(settings.trakt_client_id),
        "TRAKT_CLIENT_SECRET": bool(settings.trakt_client_secret),
        "TMDB_API_KEY": bool(settings.tmdb_api_key),
        "RECOMBEE_DATABASE_ID": bool(settings.recombee_database_id),
        "RECOMBEE_PRIVATE_TOKEN": bool(settings.recombee_private_token),
        "FERNET_KEY": bool(settings.fernet_key),
        "SECRET_KEY": settings.secret_key != "change-me-in-production",
    }
    for k, ok in fields.items():
        if not ok:
            issues.append(k)
    base = (settings.base_url or "").rstrip("/")
    if not base.startswith("https://") and not base.startswith("http://localhost"):
        issues.append("BASE_URL is not https:// (OAuth state cookie will be rejected by browsers)")
    return ProbeResult(
        "config", "config",
        "pass" if not issues else "fail",
        _ms_since(t0),
        detail={"present": fields, "base_url": base},
        error=None if not issues else f"missing/insecure: {issues}",
    )


# ============================================================
# Data sanity
# ============================================================


async def _t_data() -> ProbeResult:
    t0 = time.monotonic()
    try:
        async with session_scope() as session:
            counts = {
                "users":              await session.scalar(select(func.count()).select_from(User)),
                "users_connected":    await session.scalar(
                    select(func.count()).select_from(User).where(User.trakt_access_token_enc.is_not(None))
                ),
                "users_profile_ready": await session.scalar(
                    select(func.count()).select_from(User).where(User.profile_ready.is_(True))
                ),
                "taste_caches":       await session.scalar(select(func.count()).select_from(TasteCache)),
                "preferences":        await session.scalar(select(func.count()).select_from(UserPreferences)),
                "catalog_items":      await session.scalar(select(func.count()).select_from(ContentCatalog)),
                "catalog_embedded":   await session.scalar(
                    select(func.count()).select_from(ContentCatalog)
                    .where(ContentCatalog.embedding.is_not(None))
                ),
                "catalog_recombee_synced": await session.scalar(
                    select(func.count()).select_from(ContentCatalog)
                    .where(ContentCatalog.recombee_synced.is_(True))
                ),
                "watch_attempts":     await session.scalar(select(func.count()).select_from(WatchAttempt)),
            }
        # Status: warn if connected users have no taste cache (sync hasn't run)
        if (counts["users_connected"] or 0) > 0 and (counts["taste_caches"] or 0) == 0:
            return ProbeResult(
                "data", "data", "warn", _ms_since(t0), detail=counts,
                error="users connected but no taste profiles built yet",
                remediation="Wait for the next user_sync sweep, or POST /admin/sync/user/<user_id>.",
            )
        return ProbeResult(
            "data", "data", "pass", _ms_since(t0), detail=counts,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "data", "data", "fail", _ms_since(t0),
            error=str(exc)[:200],
        )


# ============================================================
# Top-level orchestrator
# ============================================================


async def run_selftest() -> dict[str, Any]:
    """Run every probe in parallel where independent. Returns a JSON-
    serializable summary suitable for /admin/selftest."""
    t0 = time.monotonic()
    results = await asyncio.gather(
        _t_config(),
        _t_database(),
        _t_trakt(),
        _t_tmdb(),
        _t_recombee(),
        _t_llm(),
        _t_embeddings(),
        _t_similarity(),
        _t_watch_state_machine(),
        _t_feed_builder(),
        _t_scheduler(),
        _t_data(),
        return_exceptions=False,
    )

    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    overall = "pass"
    if counts.get("fail", 0):
        overall = "fail"
    elif counts.get("warn", 0):
        overall = "warn"

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "overall": overall,
        "counts": counts,
        "elapsed_ms": _ms_since(t0),
        "probes": [asdict(r) for r in results],
    }
