"""lsp builtin tool — real LSP client via stdio JSON-RPC.

Connects to language servers (pyright, rust-analyzer, tsserver, gopls)
and provides definition, references, hover, and symbols.  Connections are
per-language, per-session via ContextVar — one server process per language,
reused across calls.

Zero extra dependencies: asyncio subprocess + JSON-RPC framing.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult

# ---------------------------------------------------------------------------
# LSP server commands (auto-detected, first-found wins)
# ---------------------------------------------------------------------------

_LSP_COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("pyright-langserver", "--stdio"),
    "typescript": ("typescript-language-server", "--stdio"),
    "rust": ("rust-analyzer",),
    "go": ("gopls", "serve"),
    "cpp": ("clangd", "--background-index=0"),
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
        ".c": "cpp",
        ".h": "cpp",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hxx": "cpp",
    }.get(ext, "")


def _find_lsp_command(lang: str) -> tuple[str, ...] | None:
    """Find the first available LSP server for a language."""
    if lang not in _LSP_COMMANDS:
        return None
    cmd = _LSP_COMMANDS[lang]
    exe = cmd[0]
    if shutil.which(exe):
        return cmd
    # Fallbacks
    fallbacks = {
        "python": (("jedi-language-server",),),
        "typescript": (("vtsls", "--stdio"),),
        "cpp": (("ccls",),),
    }
    for fb in fallbacks.get(lang, ()):
        if shutil.which(fb[0]):
            return fb
    return None


# ---------------------------------------------------------------------------
# Per-session LSP state (ContextVar — isolated per SessionRunner)
# ---------------------------------------------------------------------------


@dataclass
class LSPSessionState:
    """Per-session LSP connections: one client per language."""

    clients: dict[str, "LSPClient"] = field(default_factory=dict)


_current_state: contextvars.ContextVar[LSPSessionState | None] = (
    contextvars.ContextVar("lsp_current_state", default=None)
)


def _get_state() -> LSPSessionState:
    state = _current_state.get()
    if state is None:
        state = LSPSessionState()
        _current_state.set(state)
    return state


# ---------------------------------------------------------------------------
# LSPClient — manages one language server process
# ---------------------------------------------------------------------------


class LSPClient:
    """JSON-RPC 2.0 LSP client over stdio."""

    def __init__(self, command: tuple[str, ...], root_uri: str):
        self._command = command
        self._root_uri = root_uri
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False
        self._open_files: set[str] = set()
        self._capabilities: dict = {}

    async def start(self) -> None:
        """Launch the language server and perform initialize handshake."""
        if self._proc is not None:
            return

        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize
        result = await self._request("initialize", {
            "processId": None,
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": False},
                    "references": {},
                    "hover": {"contentFormat": ["plaintext"]},
                    "documentSymbol": {},
                },
            },
            "workspaceFolders": [{"uri": self._root_uri, "name": Path(self._root_uri.replace("file://", "")).name}],
        })
        self._capabilities = result.get("capabilities", {})
        self._initialized = True

        # Send initialized notification
        self._notify("initialized", {})

    async def ensure_open(self, filepath: str) -> str:
        """Send didOpen for a file, return its URI."""
        uri = Path(filepath).resolve().as_uri()
        if uri in self._open_files:
            return uri
        try:
            text = Path(filepath).read_text()
        except (OSError, UnicodeDecodeError):
            raise
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": _detect_lang(filepath),
                "version": 1,
                "text": text,
            },
        })
        self._open_files.add(uri)
        return uri

    async def definition(self, filepath: str, line: int, character: int) -> list[dict]:
        """Go to definition."""
        uri = await self.ensure_open(filepath)
        result = await self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": max(0, character)},
        })
        if result is None:
            return []
        locations: list[dict] = result if isinstance(result, list) else [result]
        return [
            {
                "uri": str(loc.get("uri", "")),
                "range": loc.get("range", {}),
                "line": int(loc.get("range", {}).get("start", {}).get("line", 0) + 1),
                "col": int(loc.get("range", {}).get("start", {}).get("character", 0) + 1),
            }
            for loc in locations
            if isinstance(loc, dict)
        ]

    async def references(self, filepath: str, line: int, character: int) -> list[dict]:
        """Find all references."""
        uri = await self.ensure_open(filepath)
        result = await self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": max(0, character)},
            "context": {"includeDeclaration": True},
        })
        if result is None:
            return []
        return [
            {
                "uri": str(loc.get("uri", "")),
                "range": loc.get("range", {}),
                "line": int(loc.get("range", {}).get("start", {}).get("line", 0) + 1),
                "col": int(loc.get("range", {}).get("start", {}).get("character", 0) + 1),
            }
            for loc in result
            if isinstance(loc, dict)
        ]

    async def hover(self, filepath: str, line: int, character: int) -> str:
        """Hover info."""
        uri = await self.ensure_open(filepath)
        result = await self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": max(0, character)},
        })
        if result is None:
            return "(no hover info)"
        contents = result.get("contents", {}) if isinstance(result, dict) else result
        if isinstance(contents, dict):
            return str(contents.get("value", contents))
        if isinstance(contents, list):
            return "\n".join(
                str(c.get("value", c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        return str(contents)

    async def symbols(self, filepath: str) -> list[dict]:
        """List document symbols."""
        uri = await self.ensure_open(filepath)
        result = await self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        if result is None or not isinstance(result, list):
            return []
        symbols = []
        for s in result:
            name = s.get("name", "")
            kind = _symbol_kind_name(s.get("kind", 0))
            line = (s.get("range", {}).get("start", {})
                     .get("line", 0) + 1) if "range" in s else (
                s.get("selectionRange", {}).get("start", {}).get("line", 0) + 1
            )
            symbols.append({"name": name, "kind": kind, "line": line})
            # Flatten children (nested symbols)
            for child in s.get("children", []):
                name = child.get("name", "")
                kind = _symbol_kind_name(child.get("kind", 0))
                cline = (child.get("range", {}).get("start", {})
                          .get("line", 0) + 1) if "range" in child else 0
                symbols.append({"name": name, "kind": kind, "line": cline})
        return symbols

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                self._notify("shutdown", {})
                await asyncio.wait_for(
                    self._request("exit", {}), timeout=2.0
                )
            except Exception:
                pass
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
        self._proc = None

    # --- JSON-RPC internals ---

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _request(self, method: str, params: dict) -> dict | list | None:
        rid = self._next_id()
        msg = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        self._send(msg)
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            self._pending.pop(rid, None)
            raise

    def _notify(self, method: str, params: dict) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._send(msg)

    def _send(self, msg: str) -> None:
        if self._proc and self._proc.stdin:
            frame = f"Content-Length: {len(msg.encode())}\r\n\r\n{msg}"
            self._proc.stdin.write(frame.encode())
            # drain handled by the event loop

    async def _read_loop(self) -> None:
        """Read JSON-RPC responses from stdout, dispatch to pending futures."""
        buf = b""
        while self._proc and self._proc.stdout:
            try:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    # Parse Content-Length header
                    header_end = buf.find(b"\r\n\r\n")
                    if header_end == -1:
                        break
                    header = buf[:header_end].decode()
                    content_length = 0
                    for line in header.split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            content_length = int(line.split(":", 1)[1].strip())
                    if content_length <= 0:
                        buf = buf[header_end + 4:]
                        continue
                    body_start = header_end + 4
                    if len(buf) < body_start + content_length:
                        break  # incomplete body
                    body = buf[body_start:body_start + content_length]
                    buf = buf[body_start + content_length:]
                    try:
                        msg = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    rid = msg.get("id")
                    if rid is not None and rid in self._pending:
                        future = self._pending.pop(rid)
                        if "error" in msg:
                            future.set_exception(
                                RuntimeError(msg["error"].get("message", "LSP error"))
                            )
                        elif not future.done():
                            future.set_result(msg.get("result"))
                    # Notifications (no id) are ignored
            except (asyncio.CancelledError, Exception):
                break


_SYMBOL_KINDS: dict[int, str] = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array",
    23: "struct", 25: "type",
}


def _symbol_kind_name(kind: int) -> str:
    return _SYMBOL_KINDS.get(kind, f"symbol({kind})")


async def _get_client(filepath: str) -> LSPClient | None:
    """Get or create an LSP client for the file's language."""
    lang = _detect_lang(filepath)
    if not lang:
        return None

    cmd = _find_lsp_command(lang)
    if not cmd:
        return None

    state = _get_state()
    if lang not in state.clients:
        p = Path(filepath).resolve()
        # Walk up to find project root (has .git, pyproject.toml, etc.)
        root = p.parent
        for parent in [p.parent] + list(p.parent.parents):
            markers = [".git", "pyproject.toml", "Cargo.toml", "go.mod", "package.json", "CMakeLists.txt", "compile_commands.json", "Makefile"]
            if any((parent / m).exists() for m in markers):
                root = parent
                break

        client = LSPClient(cmd, root.as_uri())
        await client.start()
        state.clients[lang] = client

    return state.clients[lang]


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@tool(
    "lsp",
    description="Code navigation via real LSP: symbols, definition, references, hover.",
)
async def lsp(
    action: Annotated[
        str,
        Field(description="Action: symbols, definition, references, hover"),
    ],
    filepath: Annotated[str, Field(description="File path to analyze")] = "",
    symbol: Annotated[
        str, Field(description="Symbol name for definition/references/hover")
    ] = "",
    line: Annotated[
        int, Field(description="Line number (1-indexed) for cursor position", ge=1)
    ] = 0,
    character: Annotated[
        int, Field(description="Column (1-indexed) for cursor position", ge=1)
    ] = 1,
) -> ToolResult:
    if action not in ("symbols", "definition", "references", "hover"):
        return ToolResult.error(
            f"unknown action: {action}. Use: symbols, definition, references, hover"
        )

    if not filepath:
        return ToolResult.error("filepath is required")

    p = Path(filepath).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return ToolResult.error(f"file not found: {filepath}")

    try:
        client = await _get_client(filepath)
    except Exception as e:
        return ToolResult.error(f"LSP server failed to start: {e!r}")

    if client is None:
        lang = _detect_lang(filepath)
        if not lang:
            return ToolResult.error(
                f"Unsupported file type: {filepath}. "
                f"LSP supports .py, .ts/.tsx/.js, .rs, .go, .c/.cpp/.h/.hpp"
            )
        available = _LSP_COMMANDS.get(lang, ())
        exe = available[0] if available else "N/A"
        return ToolResult.error(
            f"LSP server not found for {lang}. "
            f"Install: {exe} (e.g., 'pip install pyright' for Python)"
        )

    try:
        if action == "symbols":
            syms = await client.symbols(str(p))
            if not syms:
                return ToolResult.ok("(no symbols found)")
            out = [f"Symbols in {filepath}:"]
            for s in syms:
                out.append(f"  {s['line']:5d} [{s['kind']}] {s['name']}")
            return ToolResult.ok("\n".join(out))

        elif action == "definition":
            locs = await client.definition(str(p), line or 1, character or 1)
            if not locs:
                return ToolResult.ok(f"Definition of '{symbol or '<cursor>'}' not found")
            out = [f"Definition(s):"]
            for loc in locs:
                fname = Path(loc["uri"].replace("file://", "")).name if "file://" in loc["uri"] else loc["uri"]
                out.append(f"  {fname}:{loc['line']}:{loc['col']}")
            return ToolResult.ok("\n".join(out))

        elif action == "references":
            locs = await client.references(str(p), line or 1, character or 1)
            if not locs:
                return ToolResult.ok(f"No references to '{symbol or '<cursor>'}'")
            out = [f"References ({len(locs)} found):"]
            for loc in locs[:50]:
                fname = Path(loc["uri"].replace("file://", "")).name if "file://" in loc["uri"] else loc["uri"]
                out.append(f"  {fname}:{loc['line']}:{loc['col']}")
            if len(locs) > 50:
                out.append(f"  ... and {len(locs) - 50} more")
            return ToolResult.ok("\n".join(out))

        elif action == "hover":
            text = await client.hover(str(p), line or 1, character or 1)
            return ToolResult.ok(text)

    except RuntimeError as e:
        return ToolResult.error(f"LSP error: {e}")
    except TimeoutError:
        return ToolResult.error("LSP request timed out (30s)")
    except Exception as e:
        return ToolResult.error(f"LSP failed: {e!r}")

    return ToolResult.ok("")  # unreachable
