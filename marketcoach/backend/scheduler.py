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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend.config import settings
from backend.db import SessionLocal
from backend.agents.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _cron_timezone():
    """
    Resolve the timezone for cron-triggered jobs.

    Returns a ZoneInfo instance when settings.scheduler_timezone is a valid
    IANA name, or None when the setting is empty or invalid. APScheduler's
    CronTrigger treats timezone=None as "use the scheduler's default", which
    itself defaults to the server's local timezone — the same behaviour as
    before this setting existed, so an empty or bad value is backwards-
    compatible.

    Invalid names are logged loudly but non-fatal; the scheduler still starts.
    """
    name = (settings.scheduler_timezone or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "SCHEDULER_TIMEZONE=%r is not a valid IANA zone name. "
            "Falling back to server local timezone. Check https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            name,
        )
        return None


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


def _run_position_check() -> None:
    """
    Scheduled job: poll broker positions and auto-fire reviews on changes.

    Runs every position_poll_interval_minutes. The poll itself is cheap (one
    broker API call + a small DB diff). Anthropic is only called when an
    actual change is detected, and the per-poll cap + dedupe window keep
    cost bounded even if a lot of trades happen at once.
    """
    db = SessionLocal()
    try:
        orchestrator = Orchestrator(db)
        created = orchestrator._check_position_changes()
        if created > 0:
            logger.info("Position poll: created %d reviews", created)
    except Exception as exc:
        logger.exception("Position poll failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> None:
    cron_tz = _cron_timezone()

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
                timezone=cron_tz,
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
                timezone=cron_tz,
            ),
            id="morning_brief",
            name="Trading Advisor Morning Brief",
            replace_existing=True,
            # 3h grace — if the laptop was asleep at 06:00 but wakes by 09:00
            # we still want the brief to fire. Beyond 3h the market's already
            # open and a "pre-market" brief is no longer pre-market.
            misfire_grace_time=3 * 3600,
        )

    if settings.position_reviews_enabled:
        scheduler.add_job(
            _run_position_check,
            trigger=IntervalTrigger(minutes=settings.position_poll_interval_minutes),
            id="position_check",
            name="Position Change Polling",
            replace_existing=True,
            # Short grace — if a poll is more than one interval late, just
            # skip it. The next one will pick up any changes anyway.
            misfire_grace_time=settings.position_poll_interval_minutes * 60,
        )

    scheduler.start()
    logger.info(
        "Scheduler started — tz=%s, intelligence=%dh, weekly=%s, morning=%s, position_poll=%s",
        str(cron_tz) if cron_tz else "server-local",
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
        (
            f"every {settings.position_poll_interval_minutes}min"
            if settings.position_reviews_enabled
            else "disabled"
        ),
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
