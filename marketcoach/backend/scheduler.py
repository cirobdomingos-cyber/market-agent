"""
APScheduler setup for periodic agent runs.

The intelligence pipeline (news → analysis) runs every N hours (default 4).
The scheduler is started on FastAPI startup and stopped on shutdown.

APScheduler v3 note:
  We use AsyncIOScheduler so jobs run in the same event loop as FastAPI.
  Jobs that do DB work create their own session (not the request-scoped one)
  because APScheduler runs them outside of HTTP request context.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend.config import settings
from backend.db import SessionLocal
from backend.agents.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _run_intelligence_pipeline() -> None:
    """
    Scheduled job entry point.
    Creates its own DB session because this runs outside a request context.
    """
    db = SessionLocal()
    try:
        orchestrator = Orchestrator(db)
        results = orchestrator.run_intelligence_pipeline()
        status = orchestrator.get_pipeline_status(results)
        logger.info("Scheduled pipeline completed: %s", status)
    except Exception as exc:
        logger.exception("Scheduled pipeline failed: %s", exc)
    finally:
        db.close()


def _run_weekly_plan() -> None:
    """
    Scheduled job: generate the TradingAdvisor weekly plan and persist it.
    Runs Sunday evening by default so the plan is ready before Monday open.
    """
    db = SessionLocal()
    try:
        orchestrator = Orchestrator(db)
        result = orchestrator.run_weekly_plan(trigger="scheduled")
        logger.info(
            "Scheduled weekly plan completed: success=%s", result.success
        )
    except Exception as exc:
        logger.exception("Scheduled weekly plan failed: %s", exc)
    finally:
        db.close()


def _run_morning_brief() -> None:
    """
    Scheduled job: generate the daily pre-market brief and persist it.
    Runs weekday mornings (default 06:00 local) so the brief is on the
    user's phone when they wake up.
    """
    db = SessionLocal()
    try:
        orchestrator = Orchestrator(db)
        result = orchestrator.run_morning_brief(trigger="scheduled")
        logger.info(
            "Scheduled morning brief completed: success=%s", result.success
        )
    except Exception as exc:
        logger.exception("Scheduled morning brief failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    scheduler.add_job(
        _run_intelligence_pipeline,
        trigger=IntervalTrigger(hours=settings.agent_run_interval_hours),
        id="intelligence_pipeline",
        name="News + Analysis Pipeline",
        replace_existing=True,
        misfire_grace_time=300,  # 5 min grace period if a run is late
    )

    if settings.weekly_plan_enabled:
        scheduler.add_job(
            _run_weekly_plan,
            trigger=CronTrigger(
                day_of_week=settings.weekly_plan_day_of_week,
                hour=settings.weekly_plan_hour,
                minute=settings.weekly_plan_minute,
            ),
            id="weekly_plan",
            name="Trading Advisor Weekly Plan",
            replace_existing=True,
            # Weekly job — keep a generous grace window in case the app was
            # down when Sunday night came around. 6h = catches Sun evening
            # downtime without firing again on Monday morning.
            misfire_grace_time=6 * 3600,
        )

    if settings.morning_brief_enabled:
        scheduler.add_job(
            _run_morning_brief,
            trigger=CronTrigger(
                day_of_week=settings.morning_brief_day_of_week,
                hour=settings.morning_brief_hour,
                minute=settings.morning_brief_minute,
            ),
            id="morning_brief",
            name="Trading Advisor Morning Brief",
            replace_existing=True,
            # 3h grace — if the laptop was asleep at 06:00 but wakes by 09:00
            # we still want the brief to fire. Beyond 3h the market's already
            # open and a "pre-market" brief is no longer pre-market.
            misfire_grace_time=3 * 3600,
        )

    scheduler.start()
    logger.info(
        "Scheduler started — intelligence=%dh, weekly=%s, morning=%s",
        settings.agent_run_interval_hours,
        (
            f"{settings.weekly_plan_day_of_week} "
            f"{settings.weekly_plan_hour:02d}:{settings.weekly_plan_minute:02d}"
            if settings.weekly_plan_enabled
            else "disabled"
        ),
        (
            f"{settings.morning_brief_day_of_week} "
            f"{settings.morning_brief_hour:02d}:{settings.morning_brief_minute:02d}"
            if settings.morning_brief_enabled
            else "disabled"
        ),
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
