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
