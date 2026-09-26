"""Code links on Mermaid diagrams (ADR 0038 amendment) — validation + key matching.

A mermaid artifact version may carry ``links``: a map from a TARGET KEY in the diagram to a
place in a managed project's code. The panel renders the matched diagram elements as links;
a click opens the console's code pane (ADR 0112) on that range, or the operator's editor.

Target keys:

* a flowchart / class / state node id, or a flowchart subgraph id — exactly as written in
  the source (``A``, ``Animal``, ``Idle``);
* ``participant:<name>`` — a sequence participant or actor, by id or by display alias;
* ``msg:<n>`` — the n-th sequence message (1-based, in source order), or ``msg:<label>`` —
  the message whose label is exactly ``<label>`` (only if no other message shares it).

Every target is validated here the way ``show_code`` validates its range (tools/fs_tools.py):
the project fence (``live_project_registry``), the secret-path deny list, the file exists and
is text, the line is in range, and the note is at most one sentence. A bad link is DROPPED
with a reason the model can act on — it never fails the artifact. The kept links are stored
WITH the version (``version["links"]``), so an older version and its chat chip keep theirs.

The panel honours only what is stored here: the sandboxed diagram posts a KEY, and the shell
looks the target up in the rendered version's links — a path in a message is never trusted.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("protoagent.plugins.artifact")

LINKS_MAX = 100  # links per version — a diagram past ~15 nodes should be split anyway
KEY_MAX = 200
NOTE_MAX_FALLBACK = 280  # graph.components.CODE_REF_NOTE_MAX (the code-ref chip's note cap)
_ECHO_CHARS = 60
_REPORT_MAX = 20  # per-link lines echoed back before summarising

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# A sequence message line: `A->>B: label`, `A-->>B: label`, `A-xB: …`, `A-)B: …`, `A->B: …`,
# with optional +/- activation markers. Only used to COUNT messages and read their labels for
# the key check below — the renderer matches the drawn diagram, not this regex.
_SEQ_MSG = re.compile(r"^\s*[^\s:%][^:%]*?\s*(?:-->>|->>|-->|->|--x|-x|--\)|-\))\s*[+-]?\s*[^:]+?:(.*)$")


def _note_max() -> int:
    try:
        from graph.components import CODE_REF_NOTE_MAX

        return int(CODE_REF_NOTE_MAX)
    except Exception:  # noqa: BLE001 — a host without the code pane chip still gets a sane cap
        return NOTE_MAX_FALLBACK


def _registry():
    """The fs fence exactly as the fs tools see it right now (``None`` when this host has no
    fs toolset at all). Module-level so tests can substitute a registry over temp dirs."""
    try:
        from tools.fs_tools import live_project_registry
    except Exception:  # noqa: BLE001
        return None
    return live_project_registry()


def _echo(text: str) -> str:
    t = text.strip()
    if not t:
        return "(blank line)"
    if len(t) > _ECHO_CHARS:
        t = t[: _ECHO_CHARS - 1] + "…"
    return "`" + t.replace("`", "'") + "`"


def _coerce(links) -> tuple[dict | None, str]:
    """``links`` as the model sent it → a dict, or ``(None, why)``. Accepts a JSON string too:
    models often serialise a nested argument."""
    if links is None or links == "" or links == {}:
        return {}, ""
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except ValueError as e:
            return None, f"`links` is not valid JSON ({e.msg}) — none were attached."
    if not isinstance(links, dict):
        return None, "`links` must be an object mapping a diagram key to {project, path, line, end_line?, note?}."
    return links, ""


def _check_one(registry, key: str, spec) -> tuple[dict | None, str, str]:
    """One link → ``(stored, echo, "")`` when valid, else ``(None, "", reason)``."""
    if not isinstance(spec, dict):
        return None, "", "the target must be an object {project, path, line, end_line?, note?}"
    project = spec.get("project")
    path = spec.get("path")
    line = spec.get("line")
    end_line = spec.get("end_line", spec.get("endLine"))
    note = spec.get("note") or ""
    if not isinstance(project, str) or not project.strip():
        return None, "", "missing `project`"
    if not isinstance(path, str) or not path.strip():
        return None, "", "missing `path`"
    # bool is an int subclass — `line: true` is not line 1.
    if isinstance(line, bool) or not isinstance(line, int):
        return None, "", f"`line` must be an integer (got {line!r})"
    if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int)):
        return None, "", f"`end_line` must be an integer (got {end_line!r})"
    if not isinstance(note, str):
        return None, "", "`note` must be a string"
    note = note.strip()
    if len(note) > _note_max():
        return None, "", f"`note` is {len(note)} chars; keep it to one sentence (≤ {_note_max()})"
    if registry is None:
        return None, "", "this agent has no filesystem toolset, so there is no project to link into"
    from tools.fs_secrets import is_secret_path
    from tools.fs_view import count_lines, open_regular, read_window, sniff_binary

    try:
        target = registry.resolve(project, path)
    except ValueError as exc:
        return None, "", str(exc)
    root = registry.get(project).root
    reason = is_secret_path(path) or is_secret_path(target.relative_to(root))
    if reason:
        return None, "", f"{path} looks like a secret ({reason}) — it can't be linked"
    if not target.is_file():
        return None, "", f"no such file: {path}"
    try:
        with open_regular(target) as fh:
            if sniff_binary(fh):
                return None, "", f"{path} is a binary file"
            total = count_lines(fh)
            if line < 1 or line > total:
                return None, "", f"line {line} is out of range for {path} ({total} lines)"
            end = line if end_line is None else end_line
            if end < line:
                return None, "", f"end_line ({end}) is before line ({line})"
            end = min(end, total)
            fh.seek(0)
            first = read_window(fh, line, line).text
    except OSError as exc:
        return None, "", f"cannot read {path}: {exc}"
    rel = target.relative_to(root).as_posix()
    where = f"{rel}:{line}" if end == line else f"{rel}:{line}-{end}"
    stored = {"project": project, "path": rel, "line": line, "end_line": end, "note": note}
    return stored, f"{key} → {project}/{where} L{line}: {_echo(first)}", ""


class Checked:
    """The fs-validated half of a ``links`` argument (``check``), finished against the
    artifact's kind + source by ``finish`` once the store says what those are."""

    def __init__(self, kept: dict, echoes: list[str], dropped: list[str], error: str = "", given: bool = False):
        self.kept, self.echoes, self.dropped, self.error, self.given = kept, echoes, dropped, error, given


def check(links) -> Checked:
    """Validate every target in ``links`` against the fs fence. Touches the filesystem, so the
    tools call it BEFORE taking the store lock (it needs no store state). ``given`` is False
    when the caller passed nothing (None) — update_artifact then keeps the previous links."""
    if links is None:
        return Checked({}, [], [], given=False)
    raw, err = _coerce(links)
    if raw is None:
        return Checked({}, [], [], error=err, given=True)
    if not raw:
        return Checked({}, [], [], given=True)
    registry = _registry()
    kept: dict = {}
    echoes: list[str] = []
    dropped: list[str] = []
    items = list(raw.items())
    if len(items) > LINKS_MAX:
        dropped.append(f"{len(items) - LINKS_MAX} link(s) past the {LINKS_MAX}-link cap — split the diagram")
        items = items[:LINKS_MAX]
    for key, spec in items:
        k = key.strip() if isinstance(key, str) else ""
        if not k or len(k) > KEY_MAX or _CTRL.search(k):
            dropped.append(f"{str(key)[:40]!r}: not a usable key")
            continue
        stored, echo, why = _check_one(registry, k, spec)
        if stored is None:
            dropped.append(f"{k}: {why}")
            continue
        kept[k] = stored
        echoes.append(echo)
    return Checked(kept, echoes, dropped, given=True)


def finish(c: Checked, kind: str, code: str) -> tuple[dict, str]:
    """``(links to store, model-facing report)`` for a checked ``links`` argument on a ``kind``
    artifact whose new source is ``code``: what was attached (each target's first line echoed,
    so the model can check it pointed where it meant to), what was dropped and why, and keys
    that match nothing in the diagram. A bad link is dropped, never fatal."""
    if c.error:
        return {}, "\n" + c.error
    if not c.kept and not c.dropped:
        return {}, ""
    if kind != "mermaid":
        return {}, f"\nLinks ignored: code links are supported on mermaid artifacts only (this is {kind})."
    out = []
    if c.kept:
        out.append(f"Linked {len(c.kept)} diagram element(s) to code:")
        out += [f"  {x}" for x in c.echoes[:_REPORT_MAX]]
        if len(c.echoes) > _REPORT_MAX:
            out.append(f"  …and {len(c.echoes) - _REPORT_MAX} more")
    if c.dropped:
        out.append(f"Dropped {len(c.dropped)} link(s) (the rest of the artifact is fine):")
        out += [f"  {x}" for x in c.dropped[:_REPORT_MAX]]
    unmatched = unmatched_keys(c.kept, code)
    if unmatched:
        out.append(
            "These keys don't match anything in the diagram source, so they won't be clickable: "
            + ", ".join(unmatched[:_REPORT_MAX])
            + " — use a node id as written, participant:<name>, msg:<n> (1-based) or msg:<exact label>."
        )
    if c.dropped or unmatched:
        out.append("Fix them by passing `links` again with the corrected entries.")
    return c.kept, "\n" + "\n".join(out)


def validate_links(links, kind: str, code: str = "") -> tuple[dict, str]:
    """``check`` + ``finish`` in one call (for callers that already know kind and source)."""
    return finish(check(links), kind, code)


def _messages(code: str) -> list[str]:
    """The sequence messages' labels, in order (empty when the source isn't a sequenceDiagram)."""
    body = [ln for ln in code.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    if not body or not body[0].strip().startswith("sequenceDiagram"):
        return []
    out = []
    for ln in body[1:]:
        s = ln.strip()
        if s.split(" ", 1)[0] in {
            "participant",
            "actor",
            "Note",
            "note",
            "loop",
            "alt",
            "else",
            "opt",
            "par",
            "and",
            "critical",
            "break",
            "rect",
            "end",
            "autonumber",
            "activate",
            "deactivate",
            "box",
            "create",
            "destroy",
            "title",
            "accTitle",
            "accDescr",
        }:
            continue
        m = _SEQ_MSG.match(ln)
        if m:
            out.append(re.sub(r"<br\s*/?>", "\n", m.group(1).strip(), flags=re.I))
    return out


def unmatched_keys(links: dict, code: str) -> list[str]:
    """Keys that can't match anything in ``code`` — a cheap source-level check so the model
    hears about a typo'd node id or an out-of-range msg:N in the same reply. Conservative: it
    only flags what is certainly absent (the renderer is the real matcher)."""
    if not links or not code:
        return []
    msgs = _messages(code)
    out = []
    for key in links:
        if key.startswith("msg:"):
            ref = key[4:].strip()
            if ref.isdigit():
                if not msgs or not 1 <= int(ref) <= len(msgs):
                    out.append(key)
            elif sum(1 for m in msgs if m.replace("\n", " ") == ref.replace("\n", " ")) != 1:
                out.append(key)
            continue
        name = key[len("participant:") :].strip() if key.startswith("participant:") else key
        if not name or not re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", code):
            out.append(key)
    return out


def describe(links: dict | None) -> str:
    """The links of a version, one per line, for ``get_artifact`` (empty when there are none)."""
    if not links:
        return ""
    rows = []
    for k, t in list(links.items())[: _REPORT_MAX * 5]:
        where = f"{t.get('path')}:{t.get('line')}"
        if t.get("end_line") and t.get("end_line") != t.get("line"):
            where += f"-{t.get('end_line')}"
        rows.append(f"  {k} → {t.get('project')}/{where}" + (f" — {t['note']}" if t.get("note") else ""))
    return f"\n\nCode links ({len(links)}):\n" + "\n".join(rows)
