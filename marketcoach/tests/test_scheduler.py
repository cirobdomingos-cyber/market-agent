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
