"""APScheduler setup. Registers the four background jobs on startup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.jobs.content_sync import run_content_sync
from app.jobs.health_check import run_health_checks
from app.jobs.token_refresh import run_token_refresh
from app.jobs.user_sync import run_user_sync

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def start_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = get_scheduler()

    scheduler.add_job(
        run_content_sync,
        trigger=CronTrigger(hour=3, minute=0),
        id="content_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Sweep wakes up frequently; the per-user cadence decision is made
    # inside run_user_sync based on recent /feeds activity.
    scheduler.add_job(
        run_user_sync,
        trigger=IntervalTrigger(hours=settings.user_sync_sweep_interval_hours),
        id="user_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        run_token_refresh,
        trigger=IntervalTrigger(hours=settings.token_refresh_interval_hours),
        id="token_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Hourly sanity check — DB / Trakt / TMDB / Recombee / LLM. Quiet on
    # the happy path; logs WARNING with deep-dive detail on degradation.
    # First run fires 30 s after boot so operators see initial state in
    # logs without waiting an hour.
    scheduler.add_job(
        run_health_checks,
        trigger=IntervalTrigger(hours=1),
        id="health_check",
        next_run_time=datetime.utcnow() + timedelta(seconds=30),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Scheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
