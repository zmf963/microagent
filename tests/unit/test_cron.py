"""Tests for CronScheduler — scheduled agent prompts."""

import asyncio
import pytest
from microagent.cron.scheduler import CronScheduler, CronJob
from microagent import Agent, LLMConfig


class TestCronJob:
    def test_create_job(self):
        job = CronJob(
            name="daily-summary",
            schedule="0 9 * * *",
            prompt="Summarize today.",
            session_strategy="new",
        )
        assert job.name == "daily-summary"
        assert job.schedule == "0 9 * * *"
        assert job.session_strategy == "new"
        assert job.enabled is True  # default


class TestCronScheduler:
    async def test_add_job(self):
        """Adding a job does not start execution."""
        agent = Agent.from_config(LLMConfig(
            base_url="http://localhost/v1",
            api_key="test",
            model="test",
        ))
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="test", schedule="0 0 1 1 *", prompt="test")
        scheduler.add_job(job)
        assert len(scheduler.jobs) == 1
        await scheduler.stop()

    async def test_add_interval_job(self):
        agent = Agent.from_config(LLMConfig(
            base_url="http://localhost/v1", api_key="test", model="test",
        ))
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="ping", schedule="interval:300", prompt="ping")
        scheduler.add_job(job)
        assert len(scheduler.jobs) == 1

    async def test_remove_job(self):
        agent = Agent.from_config(LLMConfig(
            base_url="http://localhost/v1", api_key="test", model="test",
        ))
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="temp", schedule="0 0 * * *", prompt="temp")
        scheduler.add_job(job)
        scheduler.remove_job("temp")
        assert len(scheduler.jobs) == 0
        await scheduler.stop()

    async def test_start_stop(self):
        agent = Agent.from_config(LLMConfig(
            base_url="http://localhost/v1", api_key="test", model="test",
        ))
        scheduler = CronScheduler(agent=agent)
        # Start and immediately stop should not crash
        scheduler.start()
        await asyncio.sleep(0.01)
        await scheduler.stop()
