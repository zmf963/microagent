"""Tests for CronScheduler — scheduled agent prompts."""

import asyncio
from pathlib import Path

from microagent import Agent, LLMConfig
from microagent.cron.scheduler import CronJob, CronScheduler


class TestCronJob:
    def test_create_job(self):
        job = CronJob(
            name="daily-summary",
            schedule="0 9 * * *",
            prompt="Summarize today.",
        )
        assert job.name == "daily-summary"
        assert job.schedule == "0 9 * * *"
        assert job.prompt == "Summarize today."
        assert job.session_strategy == "new"
        assert job.enabled is True  # default

    def test_create_job_resume(self):
        job = CronJob(
            name="cont",
            schedule="0 9 * * *",
            prompt="Go on.",
            session_strategy="resume:last",
        )
        assert job.session_strategy == "resume:last"


class TestCronScheduler:
    async def test_add_job(self):
        """Adding a job does not start execution."""
        agent = Agent.from_config(
            LLMConfig(
                base_url="http://localhost/v1",
                api_key="test",
                model="test",
            )
        )
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="test", schedule="0 0 1 1 *", prompt="test")
        scheduler.add_job(job)
        assert len(scheduler.jobs) == 1
        await scheduler.stop()

    async def test_add_interval_job(self):
        agent = Agent.from_config(
            LLMConfig(
                base_url="http://localhost/v1",
                api_key="test",
                model="test",
            )
        )
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="ping", schedule="interval:300", prompt="ping")
        scheduler.add_job(job)
        assert len(scheduler.jobs) == 1

    async def test_add_job_rejects_bad_schedule_no_residue(self):
        """Malformed schedules must be rejected BEFORE the job lands in
        self.jobs — previously a bad cron expr on a running scheduler
        escaped as ValueError AND left the job registered-but-unscheduled."""
        import pytest
        agent = Agent.from_config(
            LLMConfig(base_url="http://localhost/v1", api_key="test", model="test")
        )
        scheduler = CronScheduler(agent=agent)
        for bad in ("not-a-cron", "interval:abc", "interval:0", "interval:-5", ""):
            with pytest.raises(ValueError, match="schedule"):
                scheduler.add_job(CronJob(name=f"j-{bad}", schedule=bad, prompt="p"))
        assert len(scheduler.jobs) == 0
        await scheduler.stop()

    async def test_add_job_accepts_valid_schedules(self):
        agent = Agent.from_config(
            LLMConfig(base_url="http://localhost/v1", api_key="test", model="test")
        )
        scheduler = CronScheduler(agent=agent)
        scheduler.add_job(CronJob(name="c", schedule="0 9 * * 1-5", prompt="p"))
        scheduler.add_job(CronJob(name="i", schedule="interval:60", prompt="p"))
        assert len(scheduler.jobs) == 2
        await scheduler.stop()

    async def test_remove_job(self):
        agent = Agent.from_config(
            LLMConfig(
                base_url="http://localhost/v1",
                api_key="test",
                model="test",
            )
        )
        scheduler = CronScheduler(agent=agent)
        job = CronJob(name="temp", schedule="0 0 * * *", prompt="temp")
        scheduler.add_job(job)
        scheduler.remove_job("temp")
        assert len(scheduler.jobs) == 0
        await scheduler.stop()

    async def test_start_stop(self):
        agent = Agent.from_config(
            LLMConfig(
                base_url="http://localhost/v1",
                api_key="test",
                model="test",
            )
        )
        scheduler = CronScheduler(agent=agent)
        scheduler.start()
        await asyncio.sleep(0.01)
        await scheduler.stop()

    def test_scheduler_with_store(self):
        """Scheduler accepts a store for resume:last support."""
        from microagent.core.store import InMemoryStore

        store = InMemoryStore()
        agent = Agent.from_config(
            LLMConfig(
                base_url="http://localhost/v1",
                api_key="test",
                model="test",
            )
        )
        scheduler = CronScheduler(agent=agent, store=store)
        assert scheduler.store is store


class TestTryAcquireLock:
    def test_acquire_and_release(self, tmp_path):
        from microagent.cron.scheduler import _try_acquire_lock, _release_lock
        lock = tmp_path / "test.lock"
        fd = _try_acquire_lock(lock, return_fd=True)
        assert fd is not None
        # Second acquire (non-blocking) while held must fail
        second = _try_acquire_lock(lock, return_fd=True)
        assert second is None
        _release_lock(fd)
        # After release, can re-acquire
        fd3 = _try_acquire_lock(lock, return_fd=True)
        assert fd3 is not None
        _release_lock(fd3)

    def test_check_only_mode(self, tmp_path):
        from microagent.cron.scheduler import _try_acquire_lock
        lock = tmp_path / "check.lock"
        result = _try_acquire_lock(lock, return_fd=False)
        assert result is True  # acquired and released immediately

    def test_creates_parent_dirs(self, tmp_path):
        from microagent.cron.scheduler import _try_acquire_lock
        lock = tmp_path / "nested" / "dir" / "lock.lock"
        fd = _try_acquire_lock(lock, return_fd=True)
        assert fd is not None
        from microagent.cron.scheduler import _release_lock
        _release_lock(fd)


class TestSaveCronOutput:
    def test_saves_markdown(self, tmp_path):
        from microagent.cron.scheduler import _save_cron_output
        path = _save_cron_output(tmp_path, "job1", "my prompt", "my response")
        import os
        assert os.path.exists(path)
        content = open(path).read()
        assert "# Cron Job: job1" in content
        assert "my prompt" in content
        assert "my response" in content

    def test_job_name_path_traversal_contained(self, tmp_path):
        """A job name with path separators must not escape base_dir."""
        from microagent.cron.scheduler import _save_cron_output
        path = _save_cron_output(tmp_path, "../../escaped", "p", "r")
        resolved = Path(path).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve())), path
        import os
        assert os.path.exists(path)
        # Nothing written outside
        assert not (tmp_path.parent / "escaped").exists()

    def test_job_name_absolute_path_contained(self, tmp_path):
        from microagent.cron.scheduler import _save_cron_output
        path = _save_cron_output(tmp_path, "/etc/cron-evil", "p", "r")
        resolved = Path(path).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve())), path

    def test_add_job_rejects_unsafe_name(self):
        """Defense in depth: add_job rejects names with path separators."""
        from microagent.cron.scheduler import CronScheduler, CronJob
        class FakeAgent:
            async def arun(self, msgs): return "ok"
        sched = CronScheduler(agent=FakeAgent(), lock_path=str("/tmp/x.lock"))
        import pytest
        with pytest.raises(ValueError, match="unsafe"):
            sched.add_job(CronJob(name="../evil", schedule="* * * * *", prompt="p"))
        assert "../evil" not in sched.jobs


class TestExecuteJob:
    async def test_new_strategy(self, monkeypatch, tmp_path):
        from microagent.cron.scheduler import CronScheduler, CronJob, _save_cron_output
        from microagent.core.types import Message

        class FakeAgent:
            async def arun(self, messages):
                assert isinstance(messages, list)
                assert messages[0].role == "user"
                assert messages[0].content == "the prompt"
                return "result text"

        scheduler = CronScheduler(agent=FakeAgent())
        job = CronJob(name="j", schedule="interval:60", prompt="the prompt")
        # monkeypatch home dir for output persistence
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        await scheduler._execute_job(job)

    async def test_resume_last_strategy(self, monkeypatch, tmp_path):
        from microagent.cron.scheduler import CronScheduler, CronJob
        from microagent.core.store import InMemoryStore
        from microagent.core.types import Message

        store = InMemoryStore()
        await store.append("last-sess", Message.user("old q"))
        await store.append("last-sess", Message.assistant("old a"))

        class FakeAgent:
            async def arun(self, messages):
                # Should include old history + job prompt
                roles = [m.role for m in messages]
                assert "user" in roles
                # history preserved
                assert any(m.content == "old q" for m in messages)
                assert any(m.content == "the prompt" for m in messages)
                return "resumed result"

        # Need list_sessions on the store — InMemoryStore may not have it
        # Use a fake store that mimics the interface
        class FakeStore:
            async def list_sessions(self):
                return ["last-sess"]
            async def load_history(self, sid):
                return [Message.user("old q"), Message.assistant("old a")]

        scheduler = CronScheduler(agent=FakeAgent(), store=FakeStore())
        job = CronJob(name="r", schedule="interval:30", prompt="the prompt", session_strategy="resume:last")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        await scheduler._execute_job(job)

    async def test_resume_with_no_sessions(self):
        from microagent.cron.scheduler import CronScheduler, CronJob

        class FakeStore:
            async def list_sessions(self):
                return []
            async def load_history(self, sid):
                return []

        class FakeAgent:
            async def arun(self, messages):
                assert len(messages) == 1
                assert messages[0].content == "prompt"
                return "ok"

        scheduler = CronScheduler(agent=FakeAgent(), store=FakeStore())
        job = CronJob(name="e", schedule="interval:10", prompt="prompt", session_strategy="resume:last")
        await scheduler._execute_job(job)

    async def test_agent_error_is_logged(self, caplog):
        import logging
        from microagent.cron.scheduler import CronScheduler, CronJob

        class BoomAgent:
            async def arun(self, messages):
                raise RuntimeError("boom")

        scheduler = CronScheduler(agent=BoomAgent())
        job = CronJob(name="boom", schedule="interval:10", prompt="x")
        with caplog.at_level(logging.ERROR):
            await scheduler._execute_job(job)
        assert any("failed" in r.message for r in caplog.records)
