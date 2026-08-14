"""Round 14 tests — Hermes-parity memory + skill learning.

Covers:
1. Agent.from_config memory=True (default) constructs provider + extractor
2. memory injection into turn context (recall → context block)
3. memory disabled → no injection
4. skip_memory (cron) → no injection, no extraction
5. memory cap eviction (MAX_MEMORIES)
6. write_approval pending gate: approve/reject
7. agent-created skills dir in default search paths
8. learn_skill (chat/dir) writes SKILL.md + provenance + usage
9. curator archive backup (tar.gz) + pin
"""

import asyncio
import json

import pytest

from microagent.core.tool import ToolRegistry
from microagent.core.types import (
    Message,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from microagent.llm.client import LLMConfig, StreamDone
from microagent.session.budget import Budget
from microagent.session.runner import SessionRunner


class _SimpleLLM:
    def __init__(self, text: str = "hello"):
        self._text = text
        self.config = LLMConfig("fake", "fake-key", "fake-model")
        self.calls: list[dict] = []

    async def stream(self, system, messages, tools):
        self.calls.append({"messages": list(messages)})
        yield TextDelta(text=self._text, kind="content")
        yield Usage(input_tokens=1, output_tokens=1)
        yield StreamDone(usage=Usage(input_tokens=1, output_tokens=1), stop_reason="stop")

    def for_model(self, m):
        return _SimpleLLM(self._text)


class _ToolLLM:
    def __init__(self, calls: list[tuple[str, str, dict]]):
        self._calls = list(calls)
        self.config = LLMConfig("fake", "fake-key", "fake-model")

    async def stream(self, system, messages, tools):
        for tid, name, args in self._calls:
            yield ToolCallDelta(id=tid, name=name, arguments=args)
        yield Usage(input_tokens=1, output_tokens=1)
        yield StreamDone(usage=Usage(input_tokens=1, output_tokens=1), stop_reason="tool_calls")

    def for_model(self, m):
        return self


class _FakeProvider:
    """Minimal MemoryProvider double with recall recording."""

    def __init__(self, memories=()):
        self._memories = tuple(memories)
        self.recall_queries: list[str] = []
        self.written: list = []

    async def prefetch(self, query: str) -> None:
        pass

    async def recall(self, query: str, k: int = 5):
        self.recall_queries.append(query)
        return self._memories[:k]

    async def sync_turn(self, session_id: str, history) -> None:
        pass

    async def batch_write(self, memories) -> None:
        self.written.extend(memories)

    async def delete(self, memory_id: str) -> None:
        pass

    async def pending_memories(self):
        return ()

    async def approve_memory(self, memory_id: str) -> None:
        pass

    async def reject_memory(self, memory_id: str) -> None:
        pass

    def system_prompt_block(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# 1. Agent.from_config memory default-on
# ---------------------------------------------------------------------------


class TestAgentMemoryDefault:
    def test_memory_true_default(self, monkeypatch, tmp_path):
        """from_config memory=True constructs a SQLiteMemoryProvider."""
        from microagent.agent import Agent

        monkeypatch.setattr(
            "pathlib.Path.home", lambda: tmp_path
        )
        agent = Agent.from_config(
            LLMConfig("fake", "fake-key", "fake-model"),
            store=None,
        )
        assert agent.runner.memory is not None
        from microagent.memory.provider import SQLiteMemoryProvider

        assert isinstance(agent.runner.memory, SQLiteMemoryProvider)
        assert agent.runner._extractor is not None

    def test_memory_false_disables(self, tmp_path):
        from microagent.agent import Agent

        agent = Agent.from_config(
            LLMConfig("fake", "fake-key", "fake-model"),
            store=None,
            memory=False,
        )
        assert agent.runner.memory is None
        assert agent.runner._extractor is None

    def test_memory_instance_used_directly(self, tmp_path):
        from microagent.agent import Agent

        prov = _FakeProvider()
        agent = Agent.from_config(
            LLMConfig("fake", "fake-key", "fake-model"),
            store=None,
            memory=prov,
        )
        assert agent.runner.memory is prov


# ---------------------------------------------------------------------------
# 2/3. memory injection + disabled
# ---------------------------------------------------------------------------


class TestMemoryInjection:
    async def test_memory_injected_into_turn(self, tmp_path):
        from microagent.memory.provider import Memory

        prov = _FakeProvider(
            memories=(Memory(id="m1", content="user prefers X", category="preference", created_at=1.0),)
        )
        llm = _SimpleLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), memory=prov)
        async for _ in runner.run_turn([Message.user("hi there")]):
            pass
        assert prov.recall_queries == ["hi there"]
        sent = "".join(str(m.content) for m in llm.calls[0]["messages"] if m.role == "user")
        assert "user prefers X" in sent
        assert "## Memory" in sent

    async def test_memory_disabled_no_injection(self, tmp_path):
        llm = _SimpleLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), memory=None)
        async for _ in runner.run_turn([Message.user("hi there")]):
            pass
        sent = "".join(str(m.content) for m in llm.calls[0]["messages"] if m.role == "user")
        assert "## Memory" not in sent

    async def test_skip_memory_no_injection(self, tmp_path):
        from microagent.memory.provider import Memory

        prov = _FakeProvider(
            memories=(Memory(id="m1", content="user prefers X", category="preference", created_at=1.0),)
        )
        llm = _SimpleLLM()
        runner = SessionRunner(llm=llm, registry=ToolRegistry([]), budget=Budget(), memory=prov)
        runner.skip_memory = True
        async for _ in runner.run_turn([Message.user("hi there")]):
            pass
        assert prov.recall_queries == []
        sent = "".join(str(m.content) for m in llm.calls[0]["messages"] if m.role == "user")
        assert "## Memory" not in sent

    async def test_recall_failure_does_not_crash(self, tmp_path):
        class _BoomProvider(_FakeProvider):
            async def recall(self, query: str, k: int = 5):
                raise RuntimeError("db gone")

        llm = _SimpleLLM()
        runner = SessionRunner(
            llm=llm, registry=ToolRegistry([]), budget=Budget(), memory=_BoomProvider()
        )
        events = []
        async for e in runner.run_turn([Message.user("hi")]):
            events.append(e)
        from microagent.core.types import TurnComplete

        assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# 5. memory cap eviction
# ---------------------------------------------------------------------------


class TestMemoryCapEviction:
    async def test_eviction_keeps_cap(self, tmp_path):
        from microagent.memory.provider import Memory, SQLiteMemoryProvider

        prov = SQLiteMemoryProvider(tmp_path / "cap.db")
        # Write 2x the cap — the oldest entries must be evicted.
        total = prov.MAX_MEMORIES + 50
        await prov.batch_write(
            tuple(
                Memory(
                    id=f"m{i}", content=f"fact number {i} unique",
                    category="fact", created_at=float(i),
                )
                for i in range(total)
            )
        )
        results = await prov.recall("fact", k=total)
        assert len(results) <= prov.MAX_MEMORIES
        # newest must survive
        assert any("unique" in m.content and f"m{total - 1}" in m.id for m in results)
        # oldest evicted
        assert not any(m.id == "m0" for m in results)


# ---------------------------------------------------------------------------
# 6. write_approval gate
# ---------------------------------------------------------------------------


class TestWriteApprovalGate:
    async def test_approval_holds_and_approves(self, tmp_path):
        from microagent.memory.provider import Memory, SQLiteMemoryProvider

        prov = SQLiteMemoryProvider(tmp_path / "gate.db")
        prov.write_approval = True
        await prov.batch_write(
            (Memory(id="m1", content="pending fact", category="fact", created_at=1.0),)
        )
        # live recall must NOT see it
        assert len(await prov.recall("pending", k=5)) == 0
        pending = await prov.pending_memories()
        assert len(pending) == 1 and pending[0].id == "m1"

        await prov.approve_memory("m1")
        assert len(await prov.pending_memories()) == 0
        assert len(await prov.recall("pending", k=5)) == 1

    async def test_reject_discards(self, tmp_path):
        from microagent.memory.provider import Memory, SQLiteMemoryProvider

        prov = SQLiteMemoryProvider(tmp_path / "gate.db")
        prov.write_approval = True
        await prov.batch_write(
            (Memory(id="m1", content="pending fact", category="fact", created_at=1.0),)
        )
        await prov.reject_memory("m1")
        assert len(await prov.pending_memories()) == 0
        assert len(await prov.recall("pending", k=5)) == 0

    async def test_approval_off_writes_directly(self, tmp_path):
        from microagent.memory.provider import Memory, SQLiteMemoryProvider

        prov = SQLiteMemoryProvider(tmp_path / "gate.db")
        assert prov.write_approval is False  # Hermes default
        await prov.batch_write(
            (Memory(id="m1", content="direct fact", category="fact", created_at=1.0),)
        )
        assert len(await prov.recall("direct", k=5)) == 1
        assert len(await prov.pending_memories()) == 0


# ---------------------------------------------------------------------------
# 7. agent-created skills in default search path
# ---------------------------------------------------------------------------


class TestAgentSkillsSearchPath:
    async def test_agent_skill_dir_in_search_paths(self, monkeypatch, tmp_path):
        from microagent.agent import Agent

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        agent = Agent.from_config(
            LLMConfig("fake", "fake-key", "fake-model"), store=None, memory=False
        )
        loader = agent.runner.skill_loader
        assert loader is not None
        agent_dir = tmp_path / ".microagent" / "skills"
        assert any(p == agent_dir for p in loader._paths)

    async def test_learned_skill_is_loadable(self, monkeypatch, tmp_path):
        from microagent.agent import Agent
        from microagent.skill.learner import _write_skill

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        _write_skill(
            tmp_path / ".microagent" / "skills",
            "round14-demo",
            "---\nname: round14-demo\ndescription: demo skill for round 14\n---\n\n# Demo\n\nDo the thing.\n",
        )
        agent = Agent.from_config(
            LLMConfig("fake", "fake-key", "fake-model"), store=None, memory=False
        )
        skills = await agent.runner.skill_loader.load()
        names = {s.name for s in skills}
        assert "round14-demo" in names


# ---------------------------------------------------------------------------
# 8. learn_skill
# ---------------------------------------------------------------------------


class _LearnLLM:
    def __init__(self, output: str):
        self.config = LLMConfig("fake", "fake-key", "fake-model")
        self._output = output

    async def stream(self, system, messages, tools):
        yield TextDelta(text=self._output, kind="content")
        yield Usage()
        yield StreamDone(usage=Usage(), stop_reason="stop")

    def for_model(self, m):
        return self


class TestLearnSkill:
    async def test_learn_chat_writes_skill(self, monkeypatch, tmp_path):
        from microagent.skill.learner import learn_skill

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        output = (
            "---\nname: learned-demo\ndescription: when the user asks about demo workflows\n"
            "---\n\n# Learned Demo\n\nRun the demo with `make demo`.\n"
        )
        result = await learn_skill(
            "we always run make demo to verify", kind="chat", llm=_LearnLLM(output),
        )
        assert "learned-demo" in result
        skill_path = tmp_path / ".microagent" / "skills" / "learned-demo" / "SKILL.md"
        assert skill_path.exists()
        prov = tmp_path / ".microagent" / "skills" / "learned-demo" / ".provenance.json"
        assert json.loads(prov.read_text())["created_by"] == "agent"
        usage = json.loads(
            (tmp_path / ".microagent" / "skills" / ".usage.json").read_text()
        )
        assert usage["learned-demo"]["state"] == "active"

    async def test_learn_dir_distills_tree(self, monkeypatch, tmp_path):
        from microagent.skill.learner import learn_skill

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "README.md").write_text("# My Tool\nRun it with `tool run`.\n")
        output = (
            "---\nname: tree-demo\ndescription: using the tree demo tool\n"
            "---\n\n# Tree Demo\n\nRun `tool run`.\n"
        )
        result = await learn_skill(str(src), kind="dir", llm=_LearnLLM(output))
        assert "tree-demo" in result
        assert (tmp_path / ".microagent" / "skills" / "tree-demo" / "SKILL.md").exists()

    async def test_learn_bad_output_reports_error(self, monkeypatch, tmp_path):
        from microagent.skill.learner import learn_skill

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = await learn_skill(
            "chat about stuff", kind="chat", llm=_LearnLLM("no frontmatter here")
        )
        assert result.startswith("[error]")

    async def test_learn_no_llm_reports_error(self, tmp_path):
        from microagent.skill.learner import learn_skill

        result = await learn_skill("stuff", kind="chat", llm=None)
        assert result.startswith("[error]")

    async def test_learn_url_blocked_internal(self, tmp_path):
        from microagent.skill.learner import learn_skill

        result = await learn_skill(
            "http://169.254.169.254/latest/meta-data/", kind="url", llm=_LearnLLM("x")
        )
        assert result.startswith("[error]")
        assert "blocked" in result or "SSRF" in result


# ---------------------------------------------------------------------------
# 9. curator backup + pin
# ---------------------------------------------------------------------------


class TestCuratorHermesParity:
    async def test_archive_creates_backup(self, tmp_path):
        from microagent.skill.curator import Curator

        skills_dir = tmp_path / "skills"
        skill = skills_dir / "old-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: old-skill\n---\nbody")
        (skill / ".provenance.json").write_text('{"created_by": "agent"}')
        usage = skills_dir / ".usage.json"
        usage.write_text(
            json.dumps({"old-skill": {"last_activity": 1.0, "state": "stale"}})
        )
        curator = Curator(stale_after_days=0, archive_after_days=0)
        await curator.run_once(skills_dir, usage)

        archive = skills_dir / ".archive"
        backups = list(archive.glob("old-skill-*.tar.gz"))
        assert len(backups) == 1
        assert (archive / "old-skill").is_dir()  # archived (never deleted)

    async def test_set_pinned_roundtrips(self, tmp_path):
        from microagent.skill.curator import Curator

        usage = tmp_path / ".usage.json"
        Curator.set_pinned(usage, "skill-a", True)
        data = Curator._load_usage(usage)
        assert data["skill-a"]["pinned"] is True
        Curator.set_pinned(usage, "skill-a", False)
        data = Curator._load_usage(usage)
        assert data["skill-a"]["pinned"] is False
