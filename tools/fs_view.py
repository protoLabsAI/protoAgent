"""Bounded, line-addressed file reads for the console code pane (ADR 0112).

``GET /api/fs/file`` and the ``show_code`` tool both need "lines N..M of this file,
plus how many lines it has", and they must agree on what a LINE is — the agent says
``line 42`` and the pane scrolls to line 42. So both use this module.

A line ends at ``\\n`` (what every editor's gutter counts), NOT ``str.splitlines``'s
wider set (``\\r``, ``\\x0c``, ``\\u2028`` …) that ``read_file`` inherits. Line endings are
preserved verbatim: a CRLF file comes back with its ``\\r\\n`` intact.

Everything is streamed in fixed-size chunks, so a 1 GB log or a single-line minified
bundle costs bounded memory: only the requested window is kept, only up to the byte
cap, and only :data:`MAX_LINE_CHARS` of any one line.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # text returned per response
MAX_LINES = 20_000  # lines returned per response
MAX_LINE_CHARS = 2_000  # chars of one line; the rest is replaced by LINE_CUT_MARKER
# Appended (before the line's own newline) where a line was cut. The console may style it.
LINE_CUT_MARKER = " … [line truncated]"
_CHUNK = 1 << 20
# UTF-8 is at most 4 bytes/char; keep enough raw bytes to decode MAX_LINE_CHARS chars.
_LINE_BYTES_KEPT = MAX_LINE_CHARS * 4 + 4


@dataclass
class Window:
    line_count: int
    start: int
    end: int  # inclusive; start - 1 when no line was returned (empty file)
    text: str
    truncated: bool


def _finish_line(buf: bytearray, overflow: bool, newline: bytes) -> tuple[str, bool]:
    """Decode one kept line, cutting it at MAX_LINE_CHARS. Returns (text, was_cut)."""
    body = bytes(buf)
    if newline == b"\r\n" and body.endswith(b"\r"):
        body = body[:-1]
    text = body.decode("utf-8", errors="replace")
    cut = overflow or len(text) > MAX_LINE_CHARS
    if cut:
        text = text[:MAX_LINE_CHARS] + LINE_CUT_MARKER
    return text + newline.decode("ascii"), cut


def split_lines(text: str, keepends: bool = False) -> list[str]:
    """Split ``text`` into lines on ``\\n`` ONLY — the numbering every fs tool shares.

    ``str.splitlines`` also breaks on ``\\r``, ``\\f``, ``\\v``, ``\\x1c``-``\\x1e``, ``\\x85``,
    ``\\u2028``/``\\u2029``, so a file with one form feed numbered differently in
    ``read_file``/``search_files`` than in the code pane and the operator's editor, and
    ``search_files``'s ``file:4`` opened the wrong row. With ``keepends`` each line keeps its
    ``\\n`` (a CRLF line keeps ``\\r\\n``); without, a CRLF line's trailing ``\\r`` is dropped
    too. A trailing newline does not start an extra empty line (``"a\\n"`` is one line).
    """
    if not text:
        return []
    parts = text.split("\n")
    last = parts.pop()  # "" when the text ends with a newline
    if keepends:
        out = [p + "\n" for p in parts]
    else:
        out = [p[:-1] if p.endswith("\r") else p for p in parts]
    if last:
        out.append(last)
    return out


class NotARegularFile(OSError):
    """The path is a directory, FIFO, socket or device — never read it."""


_BINARY_SNIFF_BYTES = 8192


def open_regular(path: Path) -> BinaryIO:
    """Open ``path`` for binary reading, refusing anything but a regular file.

    Checks the OPENED descriptor, not the path, so a file swapped for a FIFO or a symlink
    between a caller's checks and this open can't slip through: ``O_NONBLOCK`` keeps the
    open itself from blocking on a FIFO with no writer, ``O_NOFOLLOW`` refuses a final
    component that became a symlink (callers pass an already-resolved path), and ``fstat``
    must say ``S_ISREG``. Flags a platform lacks (Windows) are simply omitted.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotARegularFile(f"not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def sniff_binary(fh: BinaryIO) -> bool:
    """``grep -I`` semantics on an open file (a NUL in the first 8 KB); rewinds after."""
    head = fh.read(_BINARY_SNIFF_BYTES)
    fh.seek(0)
    return b"\x00" in head


def count_lines(src: Path | BinaryIO) -> int:
    """Number of lines — ``\\n`` count, plus one for a trailing unterminated line."""
    if isinstance(src, Path):
        with open_regular(src) as fh:
            return count_lines(fh)
    total = 0
    last = b""
    fh = src
    while chunk := fh.read(_CHUNK):
        total += chunk.count(b"\n")
        last = chunk[-1:]
    if last and last != b"\n":
        total += 1
    return total


def read_window(src: Path | BinaryIO, start: int = 1, end: int | None = None) -> Window:
    """Lines ``start..end`` (1-based, inclusive) of ``path``, capped.

    ``end`` omitted means "to EOF". The response is capped at :data:`MAX_LINES` lines and
    :data:`MAX_RESPONSE_BYTES` bytes of text (always at least one line, so a huge line
    still makes progress); ``truncated`` is True when a cap — including a per-line cut —
    kept back part of what was asked for. ``end`` in the result is the last line
    actually returned, so the caller pages on with ``start=end+1``.

    ``src`` is a path (opened with :func:`open_regular`) or an already-open binary file.
    Raises ``ValueError`` when ``start`` is past the end of a non-empty file.
    """
    if isinstance(src, Path):
        with open_regular(src) as fh:
            return read_window(fh, start, end)
    fh = src
    start = max(1, int(start))
    want_end = None if end is None else int(end)
    if want_end is not None and want_end < start:
        raise ValueError(f"end ({want_end}) is before start ({start})")
    # The line-count cap applies to what was ASKED for too: a request for 50k lines
    # returns 20k and says truncated.
    stop_at = start + MAX_LINES - 1 if want_end is None else min(want_end, start + MAX_LINES - 1)

    out: list[str] = []
    out_bytes = 0
    truncated = False
    collecting = True  # flips off once the byte cap stops collection
    line_no = 1
    cur = bytearray()
    cur_overflow = False
    cur_last = b""  # the line's last raw byte so far, kept even past a cut (CRLF detection)
    total_nl = 0
    last_byte = b""

    def take(newline: bytes) -> None:
        nonlocal out_bytes, collecting, truncated
        text, cut = _finish_line(cur, cur_overflow, newline)
        size = len(text.encode("utf-8"))
        if out and out_bytes + size > MAX_RESPONSE_BYTES:
            collecting = False
            truncated = True
            return
        out.append(text)
        out_bytes += size
        truncated = truncated or cut

    while chunk := fh.read(_CHUNK):
        last_byte = chunk[-1:]
        pieces = chunk.split(b"\n")
        for i, piece in enumerate(pieces):
            terminated = i < len(pieces) - 1
            in_window = collecting and start <= line_no <= stop_at
            if piece:
                cur_last = piece[-1:]
            if in_window and not cur_overflow:
                room = _LINE_BYTES_KEPT - len(cur)
                if len(piece) > room:
                    cur += piece[:room]
                    cur_overflow = True
                else:
                    cur += piece
            if terminated:
                total_nl += 1
                if in_window:
                    take(b"\r\n" if cur_last == b"\r" else b"\n")
                cur = bytearray()
                cur_overflow = False
                cur_last = b""
                line_no += 1
    line_count = total_nl + (1 if last_byte and last_byte != b"\n" else 0)
    # The trailing unterminated line, if there is one and it is still wanted.
    if line_count > total_nl and collecting and start <= line_no <= stop_at:
        take(b"")
    if line_count and start > line_count:
        raise ValueError(f"start {start} is past the end of the file ({line_count} lines)")
    got_end = start + len(out) - 1
    wanted_end = line_count if want_end is None else min(want_end, line_count)
    truncated = truncated or got_end < wanted_end
    return Window(line_count=line_count, start=start, end=got_end, text="".join(out), truncated=truncated)


# Shiki language ids by extension / exact filename (lower-cased). "text" otherwise.
_BY_NAME = {
    "dockerfile": "docker",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    "justfile": "just",
    ".gitignore": "gitignore",
    ".dockerignore": "gitignore",
    ".gitattributes": "text",
    ".bashrc": "bash",
    ".zshrc": "zsh",
    ".editorconfig": "ini",
    "go.mod": "go",
    "cargo.lock": "toml",
    "uv.lock": "toml",
    "pipfile": "toml",
    "gemfile": "ruby",
    "rakefile": "ruby",
}
_BY_EXT = {
    ".ts": "ts", ".mts": "ts", ".cts": "ts", ".tsx": "tsx",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "jsx",
    ".json": "json", ".jsonc": "jsonc", ".json5": "json5", ".jsonl": "json",
    ".py": "py", ".pyi": "py", ".ipynb": "json",
    ".md": "md", ".mdx": "mdx", ".markdown": "md",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".sh": "sh", ".bash": "bash", ".zsh": "zsh", ".fish": "fish", ".ps1": "powershell",
    ".rs": "rs", ".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".lua": "lua", ".dart": "dart", ".zig": "zig",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".ml": "ocaml", ".clj": "clojure",
    ".r": "r", ".jl": "julia", ".pl": "perl", ".sql": "sql", ".graphql": "graphql", ".gql": "graphql",
    ".proto": "proto", ".tf": "hcl", ".hcl": "hcl", ".nix": "nix",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".html": "html", ".htm": "html", ".xml": "xml", ".svg": "xml", ".vue": "vue", ".svelte": "svelte",
    ".astro": "astro", ".diff": "diff", ".patch": "diff", ".dockerfile": "docker", ".mk": "make",
    ".txt": "text", ".log": "log", ".csv": "csv", ".tsv": "tsv",
}


def guess_language(rel: str) -> str:
    """A Shiki language id for ``rel`` from its filename/extension; ``"text"`` fallback."""
    name = str(rel).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name in _BY_NAME:
        return _BY_NAME[name]
    if name.startswith(".env"):
        return "dotenv"
    dot = name.rfind(".")
    if dot > 0 or (dot == 0 and name.count(".") > 1):
        return _BY_EXT.get(name[dot:], "text")
    return "text"
