"""Tests for EventBus — the pub/sub observer mechanism."""

from microagent.core.event import EventBus


class TestEventBus:
    async def test_emit_to_subscriber(self):
        bus = EventBus()
        received = []
        bus.on("turn_complete", lambda sid, resp: received.append((sid, resp)))
        await bus.emit("turn_complete", "s1", "hello world")
        assert received == [("s1", "hello world")]

    async def test_multiple_subscribers(self):
        bus = EventBus()
        calls = []
        bus.on("tool_call", lambda name, *args: calls.append(name))
        bus.on("tool_call", lambda name, *args: calls.append(name))
        await bus.emit("tool_call", "bash")
        assert calls == ["bash", "bash"]

    async def test_no_subscribers_no_error(self):
        bus = EventBus()
        await bus.emit("nonexistent", "whatever")  # should not raise

    async def test_subscriber_exception_swallowed(self):
        bus = EventBus()
        bus.on("bad", lambda: (_ for _ in ()).throw(ValueError("boom")))
        await bus.emit("bad")  # should not raise

    async def test_async_callback(self):
        bus = EventBus()
        received = []

        async def async_cb(msg):
            received.append(msg)

        bus.on("test_async", async_cb)
        await bus.emit("test_async", "hi")
        assert received == ["hi"]

    async def test_sync_callback_wrapped(self):
        bus = EventBus()
        received = []

        def sync_cb(msg):
            received.append(msg)

        bus.on("test_sync", sync_cb)
        await bus.emit("test_sync", "hi")
        assert received == ["hi"]

    async def test_async_observers_run_concurrently(self):
        """Async callbacks gather() — a slow observer must not serialize
        ahead of a fast one (turn_complete emit is on the hot path)."""
        import asyncio
        import time

        bus = EventBus()
        order = []

        async def slow(msg):
            await asyncio.sleep(0.2)
            order.append("slow")

        async def fast(msg):
            order.append("fast")

        bus.on("evt", slow)
        bus.on("evt", fast)
        t0 = time.monotonic()
        await bus.emit("evt", "x")
        elapsed = time.monotonic() - t0
        assert order == ["fast", "slow"]  # concurrent, not in registration order
        assert elapsed < 0.35  # 0.2s not 0.2s+ε serialized... just bounded

    async def test_async_observer_exception_swallowed(self):
        bus = EventBus()

        async def bad(msg):
            raise ValueError("boom")

        bus.on("evt", bad)
        await bus.emit("evt", "x")  # must not raise
