"""Compact long command output before it reaches the model (#5784).

``run_command`` used to hand the model stdout+stderr cut at a flat character
limit, so an install or a passing test run spent most of that budget on ANSI
colour codes, carriage-return progress bars and thousands of near-identical
download lines. :func:`compact_output` is the pure transform that keeps only the
output that matters:

1. strip ANSI escape sequences (CSI, OSC and the short two-byte forms);
2. resolve ``\\r`` progress redraws to each line's final state;
3. collapse runs of identical / digits-only-different consecutive lines into one
   line plus ``(repeated N times)``;
4. if the result is still over ``threshold`` characters, keep a head and a tail
   of lines and replace the middle with ONE marker line naming the dropped line
   count and, when given, the spill file holding the full output.

:func:`write_spill` is the companion I/O helper: it writes the full original
output to a unique file under the running instance's data root (never inside a
managed project) so the marker can point at it. It raises on failure — the
caller falls back to a marker without a path.

Standard library only; :func:`compact_output` does no I/O and is deterministic.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import time
from pathlib import Path

__all__ = ["compact_output", "write_spill", "SPILL_MAX_BYTES"]

# OSC: ESC ] … terminated by BEL or ST (ESC \). Unterminated OSC runs to end of line.
_OSC = r"\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?"
# CSI: ESC [ params intermediates final  (colours, cursor moves, erase-line, …).
_CSI = r"\x1b\[[0-?]*[ -/]*[@-~]"
# 8-bit CSI introducer.
_CSI8 = r"\x9b[0-?]*[ -/]*[@-~]"
# Other two-byte escapes (ESC 7 / ESC 8 / ESC M / charset selects ESC ( B …).
_ESC2 = r"\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_0-9=>]"
_ANSI_RE = re.compile(f"{_OSC}|{_CSI}|{_CSI8}|{_ESC2}")

# Lines that differ only in their numbers (counters, percentages, sizes, ETAs)
# share this key and collapse together.
_DIGITS_RE = re.compile(r"\d+")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _resolve_redraw(line: str) -> str:
    """A line rewritten with ``\\r`` keeps only its final non-empty state."""
    if "\r" not in line:
        return line
    for segment in reversed(line.split("\r")):
        if segment:
            return segment
    return ""


def _collapse_repeats(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            out.append(line)
            i += 1
            continue
        key = _DIGITS_RE.sub("#", line)
        j = i + 1
        while j < n and lines[j].strip() and _DIGITS_RE.sub("#", lines[j]) == key:
            j += 1
        count = j - i
        if count == 1:
            out.append(line)
        else:
            # Keep the LAST of the run — for a progress run that is the final state.
            out.append(f"{lines[j - 1]} (repeated {count} times)")
        i = j
    return out


def _marker(dropped: int, total: int, spill_path: str | None) -> str:
    where = f"; full output saved to {spill_path}" if spill_path else ""
    return f"… [{dropped} of {total} lines omitted{where}] …"


def _head_tail(lines: list[str], threshold: int, spill_path: str | None) -> str:
    total = len(lines)
    budget = max(threshold, 0)
    # A single giant line must not eat the whole budget: clip each line so the
    # head and tail can each hold at least one.
    cap = max(budget // 4, 1)
    clipped = [ln if len(ln) <= cap else ln[: max(cap - 1, 0)] + "…" for ln in lines]

    head: list[str] = []
    used = 0
    head_budget = budget // 2
    for ln in clipped:
        cost = len(ln) + 1  # its joining newline
        if used + cost > head_budget:
            break
        head.append(ln)
        used += cost

    tail: list[str] = []
    for ln in reversed(clipped[len(head) :]):
        cost = len(ln) + 1
        if used + cost > budget:
            break
        tail.append(ln)
        used += cost
    tail.reverse()

    dropped = total - len(head) - len(tail)
    return "\n".join([*head, _marker(dropped, total, spill_path), *tail])


def compact_output(text: str, *, threshold: int, spill_path: str | None = None) -> str:
    """Return ``text`` with noise removed and, if still over ``threshold``
    characters, its middle lines replaced by one marker line.

    The kept lines total at most ``threshold`` characters (newlines included);
    only the marker is extra. ``spill_path`` — where the caller saved the full
    output, see :func:`write_spill` — is named in the marker so the agent can
    read what was dropped. Trailing newlines are not preserved.
    """
    cleaned = _strip_ansi(text or "")
    cleaned = cleaned.replace("\r\n", "\n")
    lines = [_resolve_redraw(ln) for ln in cleaned.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    lines = _collapse_repeats(lines)
    result = "\n".join(lines)
    if len(result) <= threshold:
        return result
    return _head_tail(lines, threshold, spill_path)


# --- full-output spill --------------------------------------------------------
# Modelled on plugins/execute_code/engine.py::_spill_stdout (#3701): a per-run
# unique name, a per-file size cap (with an in-file note when capped) and a TTL
# prune so spills don't pile up.
SPILL_MAX_BYTES = 5 * 1024 * 1024
_SPILL_TTL_SECONDS = 24 * 60 * 60
_SPILL_GLOB = "cmd-*.txt"


def _spill_dir() -> Path:
    from infra.paths import instance_paths

    return instance_paths().store("command-output")


def _prune_old_spills(spill_dir: Path, now: float) -> None:
    cutoff = now - _SPILL_TTL_SECONDS
    for old in spill_dir.glob(_SPILL_GLOB):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()


def write_spill(text: str, *, root: Path | None = None) -> str:
    """Write ``text`` (the FULL original output) to a new unique file and return
    its absolute path.

    The file lives under ``root`` or, by default, the running instance's data
    root (``<instance>/command-output``) — never inside a managed project. Past
    :data:`SPILL_MAX_BYTES` the content is cut and a note says so at the end of
    the file. Raises on any failure; the caller falls back to a path-less marker.
    """
    spill_dir = Path(root) if root is not None else _spill_dir()
    spill_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_spills(spill_dir, time.time())

    data = (text or "").encode("utf-8", errors="replace")
    if len(data) > SPILL_MAX_BYTES:
        data = (
            data[:SPILL_MAX_BYTES]
            + (f"\n…[spill capped at {SPILL_MAX_BYTES} bytes; the rest of the output was dropped]\n").encode()
        )

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = spill_dir / f"cmd-{stamp}-{secrets.token_hex(8)}.txt"
    # "xb": a name collision fails loudly rather than overwriting another run's spill.
    with path.open("xb") as fh:
        fh.write(data)
    return str(path.resolve())
