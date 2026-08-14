"""Extra coverage for cron/scheduler.py: schedule validation branches,
job-name sanitization, lock failure paths, duplicate adds, and
resume-message building fallbacks."""

import logging
import asyncio

import pytest

from microagent.cron import scheduler as sched
from microagent.cron.scheduler import (
    CronJob,
    CronScheduler,
    _save_cron_output,
    _sanitize_job_name,
    _try_acquire_lock,
    _validate_schedule,
)


class TestValidateSchedule:
    @pytest.mark.parametrize("bad", ["interval:abc", "interval:-5", "interval:0"])
    def test_rejects_bad_interval(self, bad):
        with pytest.raises(ValueError, match="interval"):
            _validate_schedule(bad)

    def test_rejects_bad_cron(self):
        with pytest.raises(ValueError, match="schedule"):
            _validate_schedule("not-a-cron-expression")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="schedule"):
            _validate_schedule("")

    def test_accepts_valid_cron(self):
        _validate_schedule("0 9 * * 1-5")

    def test_accepts_valid_interval(self):
        _validate_schedule("interval:300")


class TestSanitizeJobName:
    def test_empty_becomes_unnamed(self):
        assert _sanitize_job_name("") == "unnamed"
        assert _sanitize_job_name("...") == "unnamed"

    def test_dots_and_slashes(self):
        assert _sanitize_job_name("a/b\\c.d") == "a_b_c.d"

    def test_leading_dots_stripped(self):
        assert _sanitize_job_name("../hidden") == "hidden"


class TestSaveCronOutputMore:
    def test_returns_path_inside_base(self, tmp_path):
        path = _save_cron_output(tmp_path, "job..name", "p", "r")
        assert path.startswith(str(tmp_path / "output"))

    def test_unnamed_job_sanitized(self, tmp_path):
        path = _save_cron_output(tmp_path, "///", "p", "r")
        assert "unnamed" in path


class TestTryAcquireLockMore:
    def test_oserror_path(self, monkeypatch, tmp_path):
        def _boom(path, mode):
            raise OSError("permission denied")
        monkeypatch.setattr("builtins.open", _boom)
        assert _try_acquire_lock(tmp_path / "x.lock", return_fd=False) is False

    def test_oserror_path_return_fd(self, monkeypatch, tmp_path):
        def _boom(path, mode):
            raise OSError("permission denied")
        monkeypatch.setattr("builtins.open", _boom)
        assert _try_acquire_lock(tmp_path / "x.lock", return_fd=True) is None

    def test_no_fcntl_degradation(self, monkeypatch, tmp_path):
        """On platforms without fcntl, locking degrades to permissive."""
        monkeypatch.setattr(sched, "fcntl", None)
        lock = tmp_path / "nolock.lock"
        assert _try_acquire_lock(lock, return_fd=False) is True
        fd = _try_acquire_lock(lock, return_fd=True)
        assert fd is not None
        sched._release_lock(fd)

    def test_release_lock_close_failure_logged(self, monkeypatch, tmp_path, caplog):
        lock = tmp_path / "rel.lock"
        fd = _try_acquire_lock(lock, return_fd=True)
        assert fd is not None

        def _bad_close():
            raise OSError("close failed")

        monkeypatch.setattr(fd, "close", _bad_close)
        with caplog.at_level(logging.WARNING):
            sched._release_lock(fd)
        assert any("close" in r.message for r in caplog.records)

    def test_release_lock_unlock_failure_swallowed(self, monkeypatch, tmp_path):
        lock = tmp_path / "unl.lock"
        fd = _try_acquire_lock(lock, return_fd=True)
        assert fd is not None

        def _bad_flock(*args, **kwargs):
            raise OSError("flock broken")

        monkeypatch.setattr(sched.fcntl, "flock", _bad_flock)
        sched._release_lock(fd)

    def test_release_lock_no_fcntl_skips_unlock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sched, "fcntl", None)
        lock = tmp_path / "plain.lock"
        lock.write_text("")
        fd = open(lock, "r")
        sched._release_lock(fd)


class _FakeAgent:
    def __init__(self):
        self.calls = []

    async def arun(self, messages):
        self.calls.append(list(messages))
        return "ok"


class _FakeScheduler:
    last = None

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=False):
        _FakeScheduler.last = id

    def shutdown(self, wait=False):
        pass


class TestSchedulerMore:
    async def test_add_job_duplicate_replaces(self):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent)
        s.add_job(CronJob(name="dup", schedule="interval:60", prompt="a"))
        s.add_job(CronJob(name="dup", schedule="interval:60", prompt="b"))
        assert len(s.jobs) == 1
        assert s.jobs["dup"].prompt == "b"
        await s.stop()

    async def test_add_job_unsafe_dot_names(self):
        s = CronScheduler(agent=_FakeAgent())
        for name in ("..", ".", ""):
            with pytest.raises(ValueError, match="unsafe"):
                s.add_job(CronJob(name=name, schedule="interval:60", prompt="p"))
        assert len(s.jobs) == 0
        await s.stop()

    async def test_add_job_unsafe_backslash(self):
        s = CronScheduler(agent=_FakeAgent())
        with pytest.raises(ValueError, match="unsafe"):
            s.add_job(CronJob(name="a\\b", schedule="interval:60", prompt="p"))
        await s.stop()

    async def test_remove_job_unknown_no_crash(self):
        s = CronScheduler(agent=_FakeAgent())
        s.remove_job("missing")
        assert len(s.jobs) == 0
        await s.stop()

    async def test_remove_job_with_running_scheduler(self):
        s = CronScheduler(agent=_FakeAgent())
        s.add_job(CronJob(name="gone", schedule="interval:60", prompt="p"))

        class _RemovingScheduler:
            def __init__(self):
                self.removed = []

            def remove_job(self, job_id):
                raise RuntimeError("jobstore broken")

            def shutdown(self, wait=False):
                pass

        s._scheduler = _RemovingScheduler()  # type: ignore[assignment]
        s.remove_job("gone")
        assert "gone" not in s.jobs
        await s.stop()

    async def test_remove_job_scheduler_success(self):
        s = CronScheduler(agent=_FakeAgent())
        s.add_job(CronJob(name="gone2", schedule="interval:60", prompt="p"))

        class _RemovingScheduler:
            def __init__(self):
                self.removed = []

            def remove_job(self, job_id):
                self.removed.append(job_id)

            def shutdown(self, wait=False):
                pass

        sch = _RemovingScheduler()
        s._scheduler = sch  # type: ignore[assignment]
        s.remove_job("gone2")
        assert sch.removed == ["gone2"]
        await s.stop()

    async def test_start_schedules_existing_enabled_jobs(self, tmp_path):
        s = CronScheduler(agent=_FakeAgent(), lock_path=str(tmp_path / "s.lock"))
        s.add_job(CronJob(name="a", schedule="interval:60", prompt="p"))
        s.add_job(CronJob(name="b", schedule="interval:60", prompt="p", enabled=False))
        s._schedule_job = lambda job: scheduled.append(job.name)
        scheduled = []
        s.start()
        try:
            assert sorted(scheduled) == ["a"]
        finally:
            await s.stop()

    async def test_start_releases_lock_on_stop(self, tmp_path):
        s = CronScheduler(agent=_FakeAgent(), lock_path=str(tmp_path / "s.lock"))
        s.start()
        assert s._lock_fd is not None
        await s.stop()
        assert s._lock_fd is None

    async def test_stop_without_start(self):
        s = CronScheduler(agent=_FakeAgent())
        await s.stop()

    def test_session_id_for(self):
        job = CronJob(name="my job/name", schedule="interval:60", prompt="p")
        assert CronScheduler._session_id_for(job) == "cron-my_job_name"

    async def test_start_skips_when_lock_held(self, monkeypatch, caplog):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent, lock_path="/tmp/nonexistent.lock")
        monkeypatch.setattr(
            "microagent.cron.scheduler._try_acquire_lock",
            lambda lock_file, return_fd=False: None,
        )
        with caplog.at_level(logging.WARNING):
            s.start()
        assert not s._started
        assert any("lock" in r.message.lower() for r in caplog.records)
        await s.stop()

    async def test_add_job_schedules_when_started(self, monkeypatch):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent, lock_path="/tmp/nonexistent.lock")
        monkeypatch.setattr(
            "microagent.cron.scheduler._try_acquire_lock",
            lambda lock_file, return_fd=False: None,
        )
        s._scheduler = _FakeScheduler()  # type: ignore[assignment]
        s._started = True
        s.add_job(CronJob(name="live", schedule="interval:60", prompt="p"))
        assert "live" in s.jobs
        assert _FakeScheduler.last is not None
        await s.stop()

    async def test_schedule_job_cron_trigger(self):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent)
        s._scheduler = _FakeScheduler()  # type: ignore[assignment]
        _FakeScheduler.last = None
        s._schedule_job(CronJob(name="cronjob", schedule="0 9 * * 1-5", prompt="p"))
        assert _FakeScheduler.last == "cronjob"

    async def test_add_job_schedules_interval_when_started(self):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent)
        s._scheduler = _FakeScheduler()  # type: ignore[assignment]
        s._started = True
        _FakeScheduler.last = None
        s.add_job(CronJob(name="ticker", schedule="interval:30", prompt="p"))
        assert _FakeScheduler.last == "ticker"
        await s.stop()

    async def test_add_job_disabled_not_scheduled(self):
        agent = _FakeAgent()
        s = CronScheduler(agent=agent)
        s._scheduler = _FakeScheduler()  # type: ignore[assignment]
        s._started = True
        _FakeScheduler.last = None
        s.add_job(CronJob(name="off", schedule="interval:30", prompt="p", enabled=False))
        assert "off" in s.jobs
        assert _FakeScheduler.last is None
        await s.stop()

    async def test_build_resume_messages_empty_history(self):
        from microagent.core.types import Message

        class _Store:
            async def load_history(self, sid):
                return []

        s = CronScheduler(agent=_FakeAgent(), store=_Store())
        job = CronJob(name="j", schedule="interval:60", prompt="tick")
        msgs = await s._build_resume_messages(job)
        assert len(msgs) == 1
        assert msgs[0].content == "tick"
        assert msgs[0].role == "user"

    async def test_build_resume_messages_store_exception(self):
        from microagent.core.types import Message

        class _BadStore:
            async def load_history(self, sid):
                raise RuntimeError("store down")

        s = CronScheduler(agent=_FakeAgent(), store=_BadStore())
        job = CronJob(name="j", schedule="interval:60", prompt="tick")
        msgs = await s._build_resume_messages(job)
        assert len(msgs) == 1
        assert msgs[0].content == "tick"

    async def test_build_resume_messages_appends_prompt(self):
        from microagent.core.types import Message

        class _Store:
            async def load_history(self, sid):
                assert sid == "cron-j"
                return [Message.user("old"), Message.assistant("old-a")]

        s = CronScheduler(agent=_FakeAgent(), store=_Store())
        job = CronJob(name="j", schedule="interval:60", prompt="tick")
        msgs = await s._build_resume_messages(job)
        assert [m.content for m in msgs] == ["old", "old-a", "tick"]

    async def test_execute_job_without_runner(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        agent = _FakeAgent()
        s = CronScheduler(agent=agent)
        await s._execute_job(CronJob(name="norunner", schedule="interval:60", prompt="p"))
        assert agent.calls
        assert (tmp_path / ".microagent" / "cron" / "output" / "norunner").exists()
        await s.stop()

    async def test_execute_job_persist_failure_logged(self, monkeypatch, caplog):
        class _BoomSaveAgent:
            async def arun(self, messages):
                return "x"

        s = CronScheduler(agent=_BoomSaveAgent())
        monkeypatch.setattr(
            "microagent.cron.scheduler._save_cron_output",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        with caplog.at_level(logging.WARNING):
            await s._execute_job(CronJob(name="j", schedule="interval:60", prompt="p"))
        assert any("persist" in r.message for r in caplog.records)
        await s.stop()
