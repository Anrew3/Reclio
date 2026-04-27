"""FastAPI app entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import get_settings
from app.database import init_db, session_scope
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.routers import admin as admin_router
from app.routers import ask as ask_router
from app.routers import chilllink as chilllink_router
from app.routers import onboarding as onboarding_router
from app.routers import portal as portal_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reclio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting Reclio (base_url=%s)", settings.base_url)

    # Database
    await init_db()
    logger.info("Database initialized")

    # Vector store — lazy init on first use, but prime it in background
    async def _warmup():
        try:
            from app.services import vector_store

            await vector_store._init()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector store warmup failed: %s", exc)

        try:
            from app.services.recombee import get_recombee

            await get_recombee().initialize_schema()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Recombee schema init failed: %s", exc)

        try:
            from app.services.llm import get_llm

            llm = get_llm()
            logger.info("LLM provider: %s (enabled=%s)", llm.name, llm.enabled)
            await llm.warmup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM warmup failed: %s", exc)

    asyncio.create_task(_warmup())

    # Scheduler
    start_scheduler()

    logger.info("Reclio ready")
    try:
        yield
    finally:
        logger.info("Shutting down")
        stop_scheduler()


app = FastAPI(
    title="Reclio",
    description="ChillLink addon server: Netflix-style personalized recommendations powered by Trakt.",
    version="1.6.3",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------- #
# Health check
# --------------------------------------------------------------------- #
# Hit every 30s by Docker (see compose healthcheck). Each downstream check
# is capped at ~2.5s and runs in parallel so the whole call stays < 3s.
#
# 200 = container should keep running.  503 = container is genuinely broken.
# Only the database failing is "broken" — Trakt/TMDB/Recombee/LLM going dark
# leaves the app degraded-but-functional (cached data still serves /feeds),
# so they show up as `ok: false` in the body but don't trip the HTTP code.

_HEALTH_TIMEOUT = 2.5


async def _check_db() -> dict[str, Any]:
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


async def _check_trakt() -> dict[str, Any]:
    try:
        from app.services.trakt import get_trakt

        client = get_trakt()
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(c.get("/genres/movies"), timeout=_HEALTH_TIMEOUT)
        ok = resp.status_code == 200
        return {"ok": ok, "status": resp.status_code}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


async def _check_tmdb() -> dict[str, Any]:
    try:
        from app.services.tmdb import get_tmdb

        client = get_tmdb()
        if not client._api_key:  # noqa: SLF001
            return {"ok": False, "error": "TMDB_API_KEY not set"}
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(
            c.get("/configuration", params={"api_key": client._api_key}),  # noqa: SLF001
            timeout=_HEALTH_TIMEOUT,
        )
        return {"ok": resp.status_code == 200, "status": resp.status_code}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


async def _check_recombee() -> dict[str, Any]:
    try:
        from app.services.recombee import get_recombee

        return {"ok": get_recombee().available}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


async def _check_llm() -> dict[str, Any]:
    try:
        from app.services.llm import get_llm

        llm = get_llm()
        # We don't actually call .generate() here — that costs tokens / time.
        # Surfacing provider + enabled flag is enough for ops visibility.
        return {"ok": True, "provider": llm.name, "enabled": llm.enabled}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


@app.get("/health")
async def health() -> JSONResponse:
    db, trakt, tmdb, recombee, llm = await asyncio.gather(
        _check_db(),
        _check_trakt(),
        _check_tmdb(),
        _check_recombee(),
        _check_llm(),
    )
    checks = {"db": db, "trakt": trakt, "tmdb": tmdb, "recombee": recombee, "llm": llm}
    # Only DB failure marks the container unhealthy — every other backend has
    # a graceful-degradation path inside the app.
    healthy = bool(db.get("ok"))
    degraded = not all(c.get("ok") for c in checks.values())
    body = {
        "status": "ok" if healthy else "unhealthy",
        "degraded": degraded,
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


# Routers
app.include_router(chilllink_router.router)
app.include_router(portal_router.router)
app.include_router(onboarding_router.router)
app.include_router(ask_router.router)
app.include_router(admin_router.router)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
