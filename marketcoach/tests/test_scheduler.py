"""
Tests for the APScheduler job registration.

We don't start the scheduler — that would fire real jobs. Instead we inspect
the job list after start_scheduler() to confirm the cron trigger is wired up
with the expected day/hour.
"""

from unittest.mock import patch

from apscheduler.triggers.cron import CronTrigger

from backend import scheduler as scheduler_module
from backend.config import settings


class TestWeeklyPlanSchedule:
    def teardown_method(self):
        # Always leave the module-level scheduler in a clean state so other
        # tests don't observe leaked jobs.
        if scheduler_module.scheduler.running:
            scheduler_module.scheduler.shutdown(wait=False)
        for job in list(scheduler_module.scheduler.get_jobs()):
            job.remove()

    def test_weekly_plan_job_registered_when_enabled(self):
        with patch.object(settings, "weekly_plan_enabled", True), \
             patch.object(settings, "weekly_plan_day_of_week", "sun"), \
             patch.object(settings, "weekly_plan_hour", 21), \
             patch.object(settings, "weekly_plan_minute", 0):
            scheduler_module.start_scheduler()

            job = scheduler_module.scheduler.get_job("weekly_plan")
            assert job is not None
            assert job.name == "Trading Advisor Weekly Plan"

            trigger = job.trigger
            assert isinstance(trigger, CronTrigger)
            # Field order: year, month, day, week, day_of_week, hour, minute, second
            fields = {f.name: str(f) for f in trigger.fields}
            assert fields["day_of_week"] == "sun"
            assert fields["hour"] == "21"
            assert fields["minute"] == "0"

    def test_weekly_plan_job_not_registered_when_disabled(self):
        with patch.object(settings, "weekly_plan_enabled", False):
            scheduler_module.start_scheduler()
            assert scheduler_module.scheduler.get_job("weekly_plan") is None
            # Intelligence pipeline should still be registered
            assert scheduler_module.scheduler.get_job("intelligence_pipeline") is not None


class TestMorningBriefSchedule:
    def teardown_method(self):
        if scheduler_module.scheduler.running:
            scheduler_module.scheduler.shutdown(wait=False)
        for job in list(scheduler_module.scheduler.get_jobs()):
            job.remove()

    def test_morning_brief_job_registered_when_enabled(self):
        with patch.object(settings, "morning_brief_enabled", True), \
             patch.object(settings, "morning_brief_day_of_week", "mon-fri"), \
             patch.object(settings, "morning_brief_hour", 6), \
             patch.object(settings, "morning_brief_minute", 0):
            scheduler_module.start_scheduler()

            job = scheduler_module.scheduler.get_job("morning_brief")
            assert job is not None
            assert job.name == "Trading Advisor Morning Brief"

            trigger = job.trigger
            assert isinstance(trigger, CronTrigger)
            fields = {f.name: str(f) for f in trigger.fields}
            assert fields["day_of_week"] == "mon-fri"
            assert fields["hour"] == "6"
            assert fields["minute"] == "0"

    def test_morning_brief_job_not_registered_when_disabled(self):
        with patch.object(settings, "morning_brief_enabled", False):
            scheduler_module.start_scheduler()
            assert scheduler_module.scheduler.get_job("morning_brief") is None
            # Other jobs should still be registered
            assert scheduler_module.scheduler.get_job("intelligence_pipeline") is not None


class TestPositionPollSchedule:
    def teardown_method(self):
        if scheduler_module.scheduler.running:
            scheduler_module.scheduler.shutdown(wait=False)
        for job in list(scheduler_module.scheduler.get_jobs()):
            job.remove()

    def test_position_poll_job_registered_when_enabled(self):
        from apscheduler.triggers.interval import IntervalTrigger
        with patch.object(settings, "position_reviews_enabled", True), \
             patch.object(settings, "position_poll_interval_minutes", 5):
            scheduler_module.start_scheduler()
            job = scheduler_module.scheduler.get_job("position_check")
            assert job is not None
            assert job.name == "Position Change Polling"
            assert isinstance(job.trigger, IntervalTrigger)
            # IntervalTrigger stores the interval as a timedelta
            assert job.trigger.interval.total_seconds() == 5 * 60

    def test_position_poll_job_not_registered_when_disabled(self):
        with patch.object(settings, "position_reviews_enabled", False):
            scheduler_module.start_scheduler()
            assert scheduler_module.scheduler.get_job("position_check") is None
            # Other jobs should still be registered
            assert scheduler_module.scheduler.get_job("intelligence_pipeline") is not None


class TestSchedulerTimezone:
    """
    SCHEDULER_TIMEZONE lets cloud deployments pin cron jobs to the user's
    local time regardless of where the server runs. Without it, a Railway
    deploy (UTC) would fire the morning brief at 03:00 BRT.

    The setting is opt-in: empty string means "use server local time"
    (pre-existing behaviour), any valid IANA zone name overrides, any
    invalid name logs a warning and falls back safely.
    """

    def teardown_method(self):
        if scheduler_module.scheduler.running:
            scheduler_module.scheduler.shutdown(wait=False)
        for job in list(scheduler_module.scheduler.get_jobs()):
            job.remove()

    def test_empty_string_returns_none(self):
        with patch.object(settings, "scheduler_timezone", ""):
            assert scheduler_module._cron_timezone() is None

    def test_whitespace_only_returns_none(self):
        with patch.object(settings, "scheduler_timezone", "   "):
            assert scheduler_module._cron_timezone() is None

    def test_valid_iana_name_returns_zoneinfo(self):
        from zoneinfo import ZoneInfo
        with patch.object(settings, "scheduler_timezone", "America/Sao_Paulo"):
            tz = scheduler_module._cron_timezone()
            assert isinstance(tz, ZoneInfo)
            assert str(tz) == "America/Sao_Paulo"

    def test_invalid_name_returns_none_with_warning(self, caplog):
        import logging
        with patch.object(settings, "scheduler_timezone", "Not/A_Real_Zone"), \
             caplog.at_level(logging.WARNING, logger="backend.scheduler"):
            tz = scheduler_module._cron_timezone()
        assert tz is None
        assert any("not a valid IANA zone name" in rec.message for rec in caplog.records)

    def test_cron_jobs_use_configured_timezone(self):
        """
        When SCHEDULER_TIMEZONE is set, both the weekly plan and the
        morning brief CronTriggers must carry the same ZoneInfo. Without
        this, the Railway deploy would be silently wrong.
        """
        from zoneinfo import ZoneInfo
        with patch.object(settings, "scheduler_timezone", "America/Sao_Paulo"), \
             patch.object(settings, "weekly_plan_enabled", True), \
             patch.object(settings, "morning_brief_enabled", True):
            scheduler_module.start_scheduler()

            weekly_job = scheduler_module.scheduler.get_job("weekly_plan")
            morning_job = scheduler_module.scheduler.get_job("morning_brief")
            assert weekly_job is not None
            assert morning_job is not None

            expected = ZoneInfo("America/Sao_Paulo")
            assert weekly_job.trigger.timezone == expected
            assert morning_job.trigger.timezone == expected
