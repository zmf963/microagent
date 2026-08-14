"""Skill data model and loader Protocol.

Skills are reusable procedural knowledge stored as SKILL.md files.
The loader Protocol provides a common interface for different skill
backends (Claude Code / agentskills.io / user).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

# ---------------------------------------------------------------------------
# CJK-aware fuzzy matching
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]+")


def _cjk_aware_ratio(query: str, target: str) -> float:
    """Compute a similarity score between query and target text.

    For CJK text, uses query-bigram coverage (fraction of the query's
    bigrams that appear in the target) instead of Jaccard. Jaccard
    divides by the union of bigrams, which is dominated by the target's
    length — a short natural query (a handful of chars) against a long
    skill description (dozens of chars) scores near zero even when it's a
    verbatim sub-phrase. Query coverage measures recall, so it survives
    long descriptions. A longest-common-substring ratio is blended in to
    reward contiguous matches. Latin text falls back to SequenceMatcher.
    """
    cjk_query = _CJK_RE.findall(query)
    cjk_target = _CJK_RE.findall(target)

    if cjk_query and cjk_target:
        q_joined = "".join(cjk_query)
        t_joined = "".join(cjk_target)
        q_bigrams = _bigrams(q_joined)
        t_bigrams = _bigrams(t_joined)
        if not q_bigrams:
            return 0.0
        # Query coverage: recall of query bigrams in target (0..1).
        coverage = len(q_bigrams & t_bigrams) / len(q_bigrams)
        # Longest common substring ratio — rewards contiguous matches.
        lcs_ratio = _lcs_len(q_joined, t_joined) / len(q_joined) if q_joined else 0.0
        # ORDERED bigram-subsequence coverage: the query's bigrams must
        # appear in the target IN ORDER (gaps allowed). Set coverage alone
        # cannot tell "测试驱动" (tests → drive) from "驱动测试" (drive →
        # tests) — the order swap is a semantic change. Subsequence
        # coverage preserves order while still tolerating the synonym
        # insertions common in natural-language queries vs long
        # descriptions. This is the closest zero-dependency approximation
        # of semantic matching; embedding retrieval would go further.
        q_bigram_seq = _bigram_sequence(q_joined)
        t_bigram_seq = _bigram_sequence(t_joined)
        subseq_ratio = (
            _lcs_subseq_len(q_bigram_seq, t_bigram_seq) / len(q_bigram_seq)
            if q_bigram_seq
            else 0.0
        )
        cjk_score = max(coverage, lcs_ratio, subseq_ratio)

        # Blend with Latin SequenceMatcher for the remaining text
        latin_query = _CJK_RE.sub("", query).strip().lower()
        latin_target = _CJK_RE.sub("", target).strip().lower()
        if latin_query and latin_target:
            latin_score = difflib.SequenceMatcher(None, latin_query, latin_target).ratio()
            # Weight by text composition
            cjk_chars = sum(1 for c in query if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff")
            cjk_ratio = cjk_chars / max(len(query), 1)
            return cjk_score * cjk_ratio + latin_score * (1 - cjk_ratio)
        return cjk_score

    # No CJK in query or target — use standard SequenceMatcher
    return difflib.SequenceMatcher(None, query, target).ratio()


def _lcs_len(a: str, b: str) -> int:
    """Length of the longest common substring of a and b (character level)."""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _bigrams(text: str) -> set[str]:
    """Extract character bigrams from text."""
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else {text}


def _bigram_sequence(text: str) -> list[str]:
    """Character bigrams as an ORDERED list (duplicates preserved)."""
    return [text[i : i + 2] for i in range(len(text) - 1)]


def _lcs_subseq_len(a: list[str], b: list[str]) -> int:
    """Length of the longest common SUBSEQUENCE (order-preserving, gaps ok).

    Unlike _lcs_len (substring), this tolerates unrelated bigrams between
    matched ones — natural-language queries against long descriptions are
    rarely verbatim, but word order still carries meaning.
    """
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    return prev[n]


# ---------------------------------------------------------------------------
# Skill + LoadedSkill data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skill:
    """A single skill definition."""

    name: str  # "deepsearch"
    namespace: str  # "claude" | "agentskills" | "user"
    description: str  # used for fuzzy matching
    body: str  # markdown body
    triggers: tuple[str, ...]  # explicit keywords for matching
    source: str  # file path or URL
    mtime: float  # for cache invalidation


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """A skill matched against a user query."""

    skill: Skill
    match_reason: str  # "keyword:deepsearch" | "fuzzy:0.83"
    match_score: float


# ---------------------------------------------------------------------------
# SkillLoader Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillLoader(Protocol):
    """Protocol for skill backends."""

    async def load(self) -> tuple[Skill, ...]: ...
    async def match(self, user_input: str) -> tuple[LoadedSkill, ...]: ...


# ---------------------------------------------------------------------------
# CompositeSkillLoader — deduplicates and ranks across backends
# ---------------------------------------------------------------------------


class CompositeSkillLoader:
    """Combines multiple skill loaders, deduplicates by namespace:name,
    and ranks results by match_score."""

    def __init__(self, backends: tuple[SkillLoader, ...] = ()):
        self._backends = backends

    async def match(self, user_input: str) -> tuple[LoadedSkill, ...]:
        all_matches: list[LoadedSkill] = []
        seen: set[str] = set()
        for backend in self._backends:
            for m in await backend.match(user_input):
                key = f"{m.skill.namespace}:{m.skill.name}"
                if key in seen:
                    continue
                seen.add(key)
                all_matches.append(m)
        all_matches.sort(key=lambda m: m.match_score, reverse=True)
        return tuple(all_matches[:5])


# ---------------------------------------------------------------------------
# ClaudeSkillLoader — reads ~/.claude/skills/<name>/SKILL.md
# ---------------------------------------------------------------------------


def _parse_skill_md(path: Path, namespace: str = "claude") -> Skill | None:
    """Parse a SKILL.md file with YAML frontmatter."""
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n*(.*)", text, re.DOTALL)
    if not m:
        return None
    try:
        front = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        # Log instead of silently returning None — a single corrupted
        # frontmatter key used to make the entire skill vanish with zero
        # indication of why. The quoting fix prevented the most common
        # trigger, but any YAML error still hides the skill.
        import logging
        logging.getLogger(__name__).warning(
            "Skipping malformed SKILL.md frontmatter in %s: %s", path, e
        )
        return None

    # YAML can parse to a list/scalar (e.g. a frontmatter body that starts
    # with '- '). front.get would then raise AttributeError and abort the
    # whole load() — one malformed skill file breaks every skill. config.py
    # already guards the same way; mirror it here.
    if not isinstance(front, dict):
        import logging
        logging.getLogger(__name__).warning(
            "Skipping SKILL.md %s: frontmatter is not a mapping (got %s)",
            path, type(front).__name__,
        )
        return None

    triggers_raw = front.get("triggers", [])
    if isinstance(triggers_raw, list):
        triggers = tuple(triggers_raw)
    elif isinstance(triggers_raw, str):
        triggers = tuple(t.strip() for t in triggers_raw.split(","))
    else:
        triggers = ()

    return Skill(
        name=front.get("name", path.parent.name),
        namespace=namespace,
        description=front.get("description", ""),
        body=m.group(2).strip(),
        triggers=triggers,
        source=str(path),
        mtime=path.stat().st_mtime,
    )


class ClaudeSkillLoader:
    """Loads skills from SKILL.md files found recursively under search paths.

    Supports both flat (``<dir>/<name>/SKILL.md``) and nested
    (``<dir>/<category>/<name>/SKILL.md``) layouts via ``rglob``.

    Match strategy:
    1. Exact keyword match on triggers (score 1.0)
    2. Fuzzy match on description (difflib, score > 0.4)
    """

    def __init__(self, search_paths: tuple[Path, ...]):
        # Accept str or Path entries — the natural Python idiom is to pass a
        # string ("~/.claude/skills"). Without conversion, str.exists() raises
        # AttributeError. Same class of bug as ToolOutputStore base_dir.
        self._paths: tuple[Path, ...] = tuple(
            Path(p).expanduser() if isinstance(p, str) else p
            for p in search_paths
        )
        # mtime-based cache. The runner calls load() up to three times per
        # turn (catalog build + match + loaded-bodies injection), and each
        # call used to rglob + read_text every SKILL.md synchronously on the
        # event loop. With a large skill tree that blocks the loop and
        # re-parses identical files every turn. The fingerprint is the
        # sorted (path, mtime, size) of every SKILL.md; only when it changes
        # do we re-read and re-parse.
        self._cached: tuple[Skill, ...] = ()
        self._fingerprint: tuple | None = None

    def _scan_fingerprint(self) -> tuple:
        """Cheap stat-only fingerprint of every SKILL.md under the paths.

        No file reads — just rglob + stat. Used to decide whether the cached
        parse is still valid.
        """
        entries: list[tuple[str, float, int]] = []
        for base in self._paths:
            if not base.exists():
                continue
            for p in sorted(base.rglob("SKILL.md")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append((str(p), st.st_mtime, st.st_size))
        return tuple(entries)

    def _parse_all(self) -> tuple[Skill, ...]:
        skills: list[Skill] = []
        for base in self._paths:
            if not base.exists():
                continue
            for skill_md in sorted(base.rglob("SKILL.md")):
                s = _parse_skill_md(skill_md)
                if s is not None:
                    skills.append(s)
        return tuple(skills)

    async def load(self) -> tuple[Skill, ...]:
        import asyncio

        loop = asyncio.get_event_loop()
        # Offload the disk scan/parse to a worker thread so a large skill
        # tree does not block the event loop (the previous sync rglob +
        # read_text ran on the loop thread).
        fingerprint = await loop.run_in_executor(None, self._scan_fingerprint)
        if fingerprint == self._fingerprint and self._cached:
            return self._cached
        skills = await loop.run_in_executor(None, self._parse_all)
        self._fingerprint = fingerprint
        self._cached = skills
        return skills

    async def match(self, user_input: str) -> tuple[LoadedSkill, ...]:
        skills = await self.load()
        matches: list[LoadedSkill] = []
        text = user_input.lower()
        for s in skills:
            # Explicit keyword match
            for kw in s.triggers:
                if kw.lower() in text:
                    matches.append(LoadedSkill(s, f"keyword:{kw}", 1.0))
                    break
            else:
                # CJK-aware fuzzy match — bigram overlap for CJK,
                # SequenceMatcher for Latin text.
                ratio = _cjk_aware_ratio(text, s.description.lower())
                if ratio > 0.4:
                    matches.append(LoadedSkill(s, f"fuzzy:{ratio:.2f}", ratio))
        return tuple(matches)
