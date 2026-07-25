"""lsp builtin tool — code navigation without LSP servers.

Uses grep + regex parsing to provide symbol search, definition lookup,
reference finding, and hover info.  No external LSP server required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# Regex patterns for common symbol definitions
_DEF_PATTERNS: dict[str, re.Pattern] = {
    "python": re.compile(
        r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)", re.MULTILINE
    ),
    "typescript": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|"
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[:=]|"
        r"^\s*(?:export\s+)?class\s+(\w+)|"
        r"^\s*(?:export\s+)?interface\s+(\w+)|"
        r"^\s*(?:export\s+)?type\s+(\w+)",
        re.MULTILINE,
    ),
    "rust": re.compile(
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)|"
        r"^\s*(?:pub\s+)?struct\s+(\w+)|"
        r"^\s*(?:pub\s+)?enum\s+(\w+)|"
        r"^\s*(?:pub\s+)?trait\s+(\w+)|"
        r"^\s*(?:pub\s+)?impl\b",
        re.MULTILINE,
    ),
    "go": re.compile(
        r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)|^\s*type\s+(\w+)\s+struct",
        re.MULTILINE,
    ),
    "java": re.compile(
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:[\w<>\[\],\s]+)\s+(\w+)\s*\(|"
        r"^\s*(?:public\s+)?class\s+(\w+)|"
        r"^\s*(?:public\s+)?interface\s+(\w+)",
        re.MULTILINE,
    ),
}


def _detect_lang(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "typescript",
        ".jsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "java",
        ".swift": "java",
    }.get(ext, "python")


def _find_symbols(text: str, lang: str) -> list[tuple[str, int, str]]:
    """Extract (name, line_number, kind) from source text."""
    pattern = _DEF_PATTERNS.get(lang, _DEF_PATTERNS["python"])
    results = []
    for i, line in enumerate(text.splitlines(), 1):
        m = pattern.search(line)
        if m:
            name = next((g for g in m.groups() if g), "")
            if name:
                # Determine kind from the matched group
                kind = "function" if "def " in line or "fn " in line or "func " in line else "class"
                results.append((name, i, kind))
    return results


@tool(
    "lsp",
    description="Code navigation: find definitions, references, and symbols. Uses grep-based analysis.",
)
async def lsp(
    action: Annotated[
        str,
        Field(
            description="Action: symbols (list all in file), "
            "definition (jump to definition), "
            "references (find all usages), "
            "hover (show function signature + nearby comments)"
        ),
    ],
    filepath: Annotated[str, Field(description="File path to analyze")] = "",
    symbol: Annotated[str, Field(description="Symbol name for definition/references/hover")] = "",
) -> ToolResult:
    if action not in ("symbols", "definition", "references", "hover"):
        return ToolResult.error(
            f"unknown action: {action}. Use: symbols, definition, references, hover"
        )

    if action in ("definition", "references", "hover") and not symbol:
        return ToolResult.error(f"symbol is required for action={action}")

    if not filepath:
        return ToolResult.error("filepath is required")

    p = Path(filepath).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return ToolResult.error(f"file not found: {filepath}")

    import asyncio

    raw = await asyncio.to_thread(p.read_bytes)
    if b"\x00" in raw:
        return ToolResult.error("binary file, cannot analyze")
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    lang = _detect_lang(filepath)

    if action == "symbols":
        syms = _find_symbols(text, lang)
        if not syms:
            return ToolResult.ok("(no symbols found)")
        out = [f"Symbols in {filepath}:"]
        for name, lineno, kind in syms[:80]:
            out.append(f"  {lineno:5d} [{kind}] {name}")
        if len(syms) > 80:
            out.append(f"  ... and {len(syms) - 80} more")
        return ToolResult.ok("\n".join(out))

    elif action == "definition":
        # Find the exact line(s) where symbol is defined
        found: list[str] = []
        pattern = _DEF_PATTERNS.get(lang, _DEF_PATTERNS["python"])
        for i, line in enumerate(lines, 1):
            m = pattern.search(line)
            if m and symbol in m.groups():
                found.append(f"  {i}: {line.strip()}")
        if not found:
            return ToolResult.ok(f"Definition of '{symbol}' not found in {filepath}")
        return ToolResult.ok(f"Definition(s) of '{symbol}' in {filepath}:\n" + "\n".join(found))

    elif action == "references":
        # Find all lines mentioning the symbol (simple text match)
        refs = []
        for i, line in enumerate(lines, 1):
            if symbol in line:
                refs.append(f"  {i}: {line.strip()}")
        if not refs:
            return ToolResult.ok(f"No references to '{symbol}' in {filepath}")
        out = [f"References to '{symbol}' in {filepath} ({len(refs)} found):"]
        out.extend(refs[:50])
        if len(refs) > 50:
            out.append(f"  ... and {len(refs) - 50} more")
        return ToolResult.ok("\n".join(out))

    elif action == "hover":
        # Show the definition line + surrounding context
        idx = None
        for i, line in enumerate(lines):
            pattern = _DEF_PATTERNS.get(lang, _DEF_PATTERNS["python"])
            m = pattern.search(line)
            if m and symbol in m.groups():
                idx = i
                break

        if idx is None:
            return ToolResult.ok(f"Symbol '{symbol}' not found in {filepath}")

        start = max(0, idx - 5)
        end = min(len(lines), idx + 15)
        context = []
        for i in range(start, end):
            marker = ">>>" if i == idx else "   "
            context.append(f"{marker} {i + 1:4d}: {lines[i]}")

        return ToolResult.ok(f"Context for '{symbol}' in {filepath}:\n" + "\n".join(context))

    return ToolResult.ok("")  # unreachable
