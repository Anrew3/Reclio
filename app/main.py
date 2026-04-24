"""FastAPI app entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.routers import admin as admin_router
from app.routers import chilllink as chilllink_router
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
    version="1.0.0",
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Routers
app.include_router(chilllink_router.router)
app.include_router(portal_router.router)
app.include_router(admin_router.router)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
