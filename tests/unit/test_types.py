"""Tests for core types: Message, ToolCall, ToolResult, Events."""

from microagent.core.types import (
    Message,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnFailed,
)


class TestMessage:
    def test_user_message(self):
        m = Message.user("hello")
        assert m.role == "user"
        assert m.content == "hello"
        assert m.tool_calls == ()
        assert m.tool_call_id is None

    def test_assistant_message_with_tool_calls(self):
        tc = ToolCall(id="call_1", name="bash", arguments={"command": "ls"})
        m = Message.assistant("thinking...", tool_calls=(tc,))
        assert m.role == "assistant"
        assert len(m.tool_calls) == 1
        assert m.tool_calls[0].name == "bash"

    def test_tool_result_message_explicit_id(self):
        r = ToolResult.ok("42")
        m = Message.tool_result(r, tool_call_id="call_1")
        assert m.role == "tool"
        assert m.content == "42"
        assert m.tool_call_id == "call_1"

    def test_to_openai_dict_user(self):
        m = Message.user("hello")
        d = m.to_openai_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_openai_dict_tool_result(self):
        r = ToolResult.ok("output")
        m = Message.tool_result(r, tool_call_id="call_42")
        d = m.to_openai_dict()
        assert d["role"] == "tool"
        assert d["tool_call_id"] == "call_42"

    def test_frozen(self):
        m = Message.user("hello")
        try:
            m.content = "changed"
            assert False, "should have raised"
        except AttributeError:
            pass


class TestToolResult:
    def test_ok(self):
        r = ToolResult.ok("content")
        assert r.content == "content"
        assert not r.is_error

    def test_error(self):
        r = ToolResult.error("something broke")
        assert r.is_error
        assert "broke" in r.content

    def test_denied(self):
        r = ToolResult.denied("no permission")
        assert r.is_error
        assert r.metadata == {"denied": True}


class TestToolCall:
    def test_to_openai_dict(self):
        tc = ToolCall(id="c1", name="read_file", arguments={"path": "/tmp/x"})
        d = tc.to_openai_dict()
        assert d["type"] == "function"
        assert d["function"]["name"] == "read_file"
        import json

        assert json.loads(d["function"]["arguments"]) == {"path": "/tmp/x"}


class TestEvents:
    def test_text_delta(self):
        e = TextDelta(text="hello")
        assert e.text == "hello"

    def test_turn_complete(self):
        e = TurnComplete(content="done")
        assert e.content == "done"

    def test_turn_failed(self):
        e = TurnFailed(reason="budget")
        assert e.reason == "budget"
