"""Tests for the fenced multi-project filesystem toolset (ADR 0007)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field

import pytest

from tools.fs_tools import Project, ProjectRegistry, build_fs_tools

_LIST_COMMAND = "dir /b" if os.name == "nt" else "ls"


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_run_requires_approval: bool = True
    filesystem_bypass_allowed: bool = True
    filesystem_projects: list = field(default_factory=list)
    tools_memoize_reads_enabled: bool = False


@pytest.fixture
def workspace(tmp_path):
    a = tmp_path / "projA"
    (a / "src").mkdir(parents=True)
    (a / "src" / "main.py").write_text("print('hello')\nTODO: fix\n")
    (a / "README.md").write_text("# A")
    b = tmp_path / "projB"
    b.mkdir()
    (b / "notes.txt").write_text("read only")
    return tmp_path, a, b


# ── registry / fence ──────────────────────────────────────────────────────────


def test_registry_resolves_within_root(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a, write=True)])
    assert reg.resolve("a", "src/main.py") == a / "src" / "main.py"
    assert reg.resolve("a", ".") == a


def test_registry_rejects_escape(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    for bad in ["../etc/passwd", "../../x", "/etc/passwd", "~/secrets"]:
        with pytest.raises(ValueError):
            reg.resolve("a", bad)


def test_registry_unknown_project(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    with pytest.raises(ValueError, match="unknown project"):
        reg.resolve("nope", ".")


# ── build_fs_tools wiring ──────────────────────────────────────────────────────


def _tools(cfg):
    return {t.name: t for t in build_fs_tools(cfg)}


def test_no_tools_without_valid_projects():
    assert build_fs_tools(_Cfg(filesystem_projects=[])) == []
    # Nonexistent path → skipped → no tools.
    assert build_fs_tools(_Cfg(filesystem_projects=[{"name": "x", "path": "/nope/zzz"}])) == []


def test_all_folders_unusable_warns_with_paths(caplog):
    """Configured-but-all-unusable unbinds every fs tool. That's an operator
    mistake, so it logs at WARNING and names the offending folder; the inert default
    (nothing configured) stays quiet at INFO."""
    with caplog.at_level(logging.WARNING, logger="protoagent.fs"):
        assert build_fs_tools(_Cfg(filesystem_projects=[{"name": "x", "path": "/nope/zzz"}])) == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NOT bound" in m and "/nope/zzz" in m for m in msgs), msgs


def test_read_list_find_search(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})
    assert "README.md" in t["list_dir"].invoke({"project": "a", "path": "."})
    assert "src/main.py" in t["find_files"].invoke({"project": "a", "pattern": "**/*.py"})
    hit = t["search_files"].invoke({"project": "a", "query": "TODO"})
    assert "main.py" in hit and "TODO" in hit


# ── search hygiene: binary + generated trees (#2541) ──────────────────────────
@pytest.fixture
def noisy_workspace(tmp_path):
    """A project whose answer is in source, surrounded by the artifacts that used to
    drown it: real marshalled bytecode, a pytest cache, and a vendored tree."""
    root = tmp_path / "noisy"
    (root / "src").mkdir(parents=True)
    (root / "src" / "store.py").write_text("SESSION_KEY = 'needle'\n")

    # A .pyc holds NUL bytes and a copy of every string literal in its source — which
    # is why it matched, and why the match was unreadable.
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "store.cpython-311.pyc").write_bytes(
        b"\xcb\r\r\n\x00\x00\x00\x00" + b"needle" + b"\x00\xe3\x00\x00"
    )
    (root / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "cache" / "nodeids").write_text('["test_needle"]')
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("// needle\n")
    return root


def test_search_skips_bytecode_and_generated_dirs(noisy_workspace):
    """The #2541 report: one search dumped several KB of bytecode into the model's
    context, and the cache hits duplicated (or stale-quoted) the source hit."""
    t = _tools(_Cfg(filesystem_projects=[{"name": "n", "path": str(noisy_workspace)}]))

    hit = t["search_files"].invoke({"project": "n", "query": "needle"})

    assert "src/store.py" in hit
    assert ".pyc" not in hit
    assert "__pycache__" not in hit
    assert ".pytest_cache" not in hit
    assert "node_modules" not in hit


def test_search_can_opt_back_into_generated_trees(noisy_workspace):
    """The escape hatch — grepping a vendored dependency is a real need."""
    t = _tools(_Cfg(filesystem_projects=[{"name": "n", "path": str(noisy_workspace)}]))

    hit = t["search_files"].invoke({"project": "n", "query": "needle", "include_generated": True})

    assert "node_modules/pkg/index.js" in hit


def test_no_match_says_what_was_not_searched(noisy_workspace):
    """'(no matches)' must not read as 'not in this repo' when the tool pruned trees."""
    t = _tools(_Cfg(filesystem_projects=[{"name": "n", "path": str(noisy_workspace)}]))

    out = t["search_files"].invoke({"project": "n", "query": "zzz-absent"})

    assert "no matches" in out and "include_generated" in out


def test_search_lists_its_exclusions_in_the_tool_description(workspace):
    """Acceptance criterion: the model has to be told what it is not being shown."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    desc = t["search_files"].description
    assert "__pycache__" in desc and "node_modules" in desc and "include_generated" in desc


# ── search_files: regex + context lines ─────────────────────────────────────


def test_search_files_literal_by_default_regex_metachars_are_not_special(workspace):
    _, a, _ = workspace
    (a / "dotted.py").write_text("a.b = 1\naxb = 2\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "a.b"})

    assert "dotted.py:1:" in out
    assert "dotted.py:2:" not in out  # "." is a literal dot, not "any char" — axb doesn't match


def test_search_files_regex_true_matches_a_pattern(workspace):
    _, a, _ = workspace
    (a / "handlers.py").write_text("def foo_handler():\n    pass\n\ndef bar():\n    pass\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": r"def \w+_handler\(", "regex": True})

    assert "handlers.py:1:" in out
    assert "bar" not in out


def test_search_files_bad_regex_is_a_clean_error(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "(unclosed", "regex": True})

    assert out.startswith("Error:") and "regex" in out


def test_search_files_context_lines_includes_surrounding_lines(workspace):
    _, a, _ = workspace
    body = "\n".join([f"line {i}" for i in range(1, 11)]) + "\n"
    body = body.replace("line 5", "TARGET")
    (a / "ctx.py").write_text(body)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "context_lines": 2})

    assert "ctx.py:5: TARGET" in out  # the match line, ":" marker
    assert "ctx.py-3- line 3" in out  # context lines, "-" marker
    assert "ctx.py-4- line 4" in out
    assert "ctx.py-6- line 6" in out
    assert "ctx.py-7- line 7" in out
    assert "line 1" not in out  # outside the window
    assert "line 9" not in out


def test_search_files_context_lines_merges_overlapping_windows(workspace):
    # Two matches 3 lines apart with context=2 overlap — must render as ONE
    # block, not duplicate the lines they share.
    _, a, _ = workspace
    lines = [f"line {i}" for i in range(1, 21)]
    lines[4] = "TARGET_A"  # line 5
    lines[7] = "TARGET_B"  # line 8
    (a / "overlap.py").write_text("\n".join(lines) + "\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "regex": True, "context_lines": 2})

    assert out.count("line 6") == 1  # shared by both windows — not duplicated
    assert out.count("line 7") == 1
    assert "--" not in out  # adjacent/overlapping ⇒ one merged block, no separator


def test_search_files_context_lines_separates_non_adjacent_blocks(workspace):
    _, a, _ = workspace
    lines = [f"line {i}" for i in range(1, 31)]
    lines[2] = "TARGET_A"  # line 3
    lines[27] = "TARGET_B"  # line 28
    (a / "apart.py").write_text("\n".join(lines) + "\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "regex": True, "context_lines": 1})

    assert "--" in out  # far apart ⇒ two separate blocks


def test_search_files_context_lines_is_clamped(workspace):
    from tools.fs_tools import _MAX_CONTEXT_LINES

    _, a, _ = workspace
    lines = [f"line {i}" for i in range(1, 51)]
    lines[24] = "TARGET"  # line 25
    (a / "clamp.py").write_text("\n".join(lines) + "\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "context_lines": 9999})

    # Clamped to _MAX_CONTEXT_LINES either side, not the whole 49-line file.
    assert out.count("\n") + 1 <= 2 * _MAX_CONTEXT_LINES + 2


def test_search_files_context_lines_total_output_is_bounded_not_just_match_count(workspace):
    # _MAX_MATCHES bounds the number of MATCHES, not the total rendered size — at
    # a wide context_lines, many sparse matches would otherwise emit far more
    # output than the old (zero-context) tool ever could. A separate output-line
    # budget has to catch this even though the match count alone stays legal.
    from tools.fs_tools import _MAX_CONTEXT_LINES, _MAX_MATCHES, _MAX_SEARCH_OUTPUT_LINES

    _, a, _ = workspace
    # Sparse matches spaced further apart than 2*context_lines so their windows
    # never merge — each of the (well under _MAX_MATCHES) matches gets its own
    # full-width context block.
    spacing = 2 * _MAX_CONTEXT_LINES + 5
    n = 30
    assert n < _MAX_MATCHES  # the match cap must NOT be what stops this
    lines = [f"line {i}" for i in range(n * spacing)]
    for k in range(n):
        lines[k * spacing] = "TARGET"
    (a / "sparse.py").write_text("\n".join(lines) + "\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "context_lines": _MAX_CONTEXT_LINES})

    assert "more matches" in out  # the output cap DID engage
    rendered = out.count("\n") + 1
    assert rendered <= _MAX_SEARCH_OUTPUT_LINES + 2  # +1 for the cutoff note, +1 slack


def test_search_files_output_cap_engages_mid_range_not_just_between_matches(workspace):
    # A single run of adjacent matches merges into ONE range — the cap must still
    # cut it off mid-range, not render the whole (possibly huge) merged block
    # before ever checking.
    from tools.fs_tools import _MAX_SEARCH_OUTPUT_LINES

    _, a, _ = workspace
    n = _MAX_SEARCH_OUTPUT_LINES + 100
    lines = ["TARGET"] * n  # every line matches — one giant merged range
    (a / "dense.py").write_text("\n".join(lines) + "\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    out = t["search_files"].invoke({"project": "a", "query": "TARGET", "context_lines": 1})

    assert "more matches" in out
    assert out.count("\n") + 1 <= _MAX_SEARCH_OUTPUT_LINES + 2


def test_search_files_regex_a_pathological_pattern_times_out_cleanly(workspace):
    # `query` is model-supplied; stdlib `re` has no match timeout, and capping the
    # matched slice's LENGTH does not help — verified separately that ~30-40
    # adversarial chars already take 70s+ against stdlib `re`, far below any sane
    # length cap. `regex`'s `timeout=` is the actual bound. This must return the
    # clean timeout error within a few seconds, not hang indefinitely.
    import time

    _, a, _ = workspace
    # (a|a)+b: confirmed empirically to blow past a 2s regex timeout at ~40 chars
    # of non-matching input — a genuinely pathological case, not a fast/optimized one.
    (a / "evil.txt").write_text("a" * 40)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))

    start = time.monotonic()
    out = t["search_files"].invoke({"project": "a", "query": r"(a|a)+b", "regex": True})
    elapsed = time.monotonic() - start

    assert elapsed < 10.0  # bounded by the 2s per-match timeout, not left to run forever
    assert out.startswith("Error:") and "too long" in out


def test_read_file_pages_past_a_line_limit(workspace):
    # A file with more lines than the default limit used to be permanently capped
    # at the first chunk — no way to see the rest in any call. offset/limit page
    # through it, line-addressed the same way search_files reports hits.
    _, a, _ = workspace
    lines = [f"line {i}\n" for i in range(1, 251)]
    # write_bytes with an explicit LF payload — write_text applies platform newline
    # translation (CRLF on Windows), which would corrupt the exact-content asserts
    # below the same way _write_exact avoids it elsewhere in this file.
    (a / "big.txt").write_bytes("".join(lines).encode("utf-8"))
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    first = t["read_file"].invoke({"project": "a", "path": "big.txt", "limit": 100})
    assert first.startswith("line 1\n")
    assert "line 100\n" in first
    assert "line 101" not in first
    assert "showing lines 1-100 of 250; call again with offset=101" in first

    second = t["read_file"].invoke({"project": "a", "path": "big.txt", "offset": 101, "limit": 100})
    assert second.startswith("line 101\n")
    assert "showing lines 101-200 of 250; call again with offset=201" in second

    third = t["read_file"].invoke({"project": "a", "path": "big.txt", "offset": 201, "limit": 100})
    assert third.startswith("".join(lines[200:]))  # the remainder
    assert "showing lines 201-250 of 250)" in third  # still oriented — no "call again", it's EOF
    assert "call again" not in third


def test_read_file_no_limit_returns_a_small_file_whole_regardless_of_line_count(workspace):
    # Regression: _DEFAULT_READ_LINES (1000) must not become a NEW truncation
    # point for a file that's objectively small in chars but has many short
    # lines — the old char-only contract returned a file like this whole in one
    # call (well under _MAX_READ_CHARS), and an unset `limit` must still honor
    # that, not silently cap at 1000 lines just because line count > line default.
    from tools.fs_tools import _MAX_READ_CHARS

    _, a, _ = workspace
    n = 2000  # > _DEFAULT_READ_LINES, but tiny in total chars
    lines = [f"{i}\n" for i in range(n)]
    body = "".join(lines).encode("utf-8")
    assert len(body) < _MAX_READ_CHARS  # sanity: genuinely small, this isn't cheating the cap
    (a / "many_short_lines.txt").write_bytes(body)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["read_file"].invoke({"project": "a", "path": "many_short_lines.txt"})

    assert out == body.decode("utf-8")  # the WHOLE file, no truncation note, no "call again"


def test_read_file_explicit_limit_still_caps_a_file_that_would_otherwise_fit_whole(workspace):
    # The auto-extension above only kicks in when `limit` is UNSET — an explicit
    # limit is a deliberate request and must be honored exactly, even though the
    # whole file would easily fit in one call.
    _, a, _ = workspace
    lines = [f"{i}\n" for i in range(50)]  # tiny — trivially fits in one call
    (a / "small.txt").write_bytes("".join(lines).encode("utf-8"))
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["read_file"].invoke({"project": "a", "path": "small.txt", "limit": 10})

    assert out.startswith("0\n1\n")
    assert "10\n" not in out  # stopped at line 10 (0-indexed content, 10 lines: 0..9)
    assert "showing lines 1-10 of 50; call again with offset=11" in out


def test_read_file_composes_with_search_files_line_numbers(workspace):
    # The actual point of the redesign: a search_files hit's line number reads
    # straight into read_file's offset/limit — no char-offset guessing.
    _, a, _ = workspace
    lines = [f"line {i}\n" for i in range(1, 51)]
    lines[29] = "TARGET\n"  # line 30 (1-indexed)
    (a / "mid.txt").write_bytes("".join(lines).encode("utf-8"))  # see LF-payload note above
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    hit = t["search_files"].invoke({"project": "a", "query": "TARGET"})
    assert "mid.txt:30:" in hit

    around = t["read_file"].invoke({"project": "a", "path": "mid.txt", "offset": 25, "limit": 10})
    assert "TARGET\n" in around
    assert around.startswith("line 25\n")


def test_read_file_a_single_line_over_the_char_cap_is_cut_short(workspace):
    from tools.fs_tools import _MAX_READ_CHARS

    _, a, _ = workspace
    (a / "minified.js").write_text("x" * (_MAX_READ_CHARS + 100))
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["read_file"].invoke({"project": "a", "path": "minified.js"})
    assert out.startswith("x" * 100)
    assert "longer than" in out and "search_files instead" in out


def test_read_file_offset_past_the_end_is_an_error(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["read_file"].invoke({"project": "a", "path": "README.md", "offset": 10_000})
    assert out.startswith("Error:") and "past the end" in out and "lines" in out


def test_read_file_offset_1_is_valid_on_an_empty_file(workspace):
    _, a, _ = workspace
    (a / "empty.txt").write_bytes(b"")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert t["read_file"].invoke({"project": "a", "path": "empty.txt"}) == ""


def test_read_file_offset_past_an_empty_file_is_still_an_error(workspace):
    # total=0 must not silently bypass the past-the-end guard — only offset=1
    # (the empty-file floor) is valid; anything past it still errors.
    _, a, _ = workspace
    (a / "empty.txt").write_bytes(b"")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["read_file"].invoke({"project": "a", "path": "empty.txt", "offset": 2})
    assert out.startswith("Error:") and "past the end" in out and "(0 lines)" in out


def test_read_file_line_truncated_still_names_the_next_offset_when_more_remains(workspace):
    # A single oversized line gets cut short, but if the FILE isn't done, the
    # caller still needs a way to discover the rest.
    from tools.fs_tools import _MAX_READ_CHARS

    _, a, _ = workspace
    body = ("x" * (_MAX_READ_CHARS + 100) + "\n" + "line 2\n").encode("utf-8")
    (a / "mixed.txt").write_bytes(body)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["read_file"].invoke({"project": "a", "path": "mixed.txt"})
    assert "longer than" in out
    assert "call again with offset=2" in out  # the file isn't done — line 2 is still there

    rest = t["read_file"].invoke({"project": "a", "path": "mixed.txt", "offset": 2})
    assert rest.startswith("line 2\n")


def test_read_file_default_offset_is_unaffected_by_the_new_param(workspace):
    # A small file's default (no offset/limit) call is byte-for-byte identical to
    # before — no truncation note, no behavior change for the common case.
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert t["read_file"].invoke({"project": "a", "path": p}) == _EXACT_CONTENT


def test_read_file_escape_is_refused(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))
    out = t["read_file"].invoke({"project": "a", "path": "../projB/notes.txt"})
    assert out.startswith("Error:") and "escape" in out


def test_write_and_edit_in_rw_project(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "Created" in t["write_file"].invoke({"project": "a", "path": "new.txt", "content": "v1"})
    assert (a / "new.txt").read_text() == "v1"
    assert "Edited" in t["edit_file"].invoke({"project": "a", "path": "new.txt", "old": "v1", "new": "v2"})
    assert (a / "new.txt").read_text() == "v2"


def test_write_blocked_in_readonly_project(workspace):
    _, _, b = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "b", "path": str(b), "write": False}]))
    out = t["write_file"].invoke({"project": "b", "path": "x.txt", "content": "nope"})
    assert out.startswith("Error:") and "read-only" in out
    assert not (b / "x.txt").exists()


def test_edit_requires_unique_old(workspace):
    _, a, _ = workspace
    (a / "dup.txt").write_text("x\nx\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["edit_file"].invoke({"project": "a", "path": "dup.txt", "old": "x", "new": "y"})
    assert out.startswith("Error:") and "not unique" in out


# ── run_command gating ─────────────────────────────────────────────────────────


def test_run_command_absent_unless_allowed(workspace):
    _, a, _ = workspace
    base = {"name": "a", "path": str(a), "write": True}
    assert "run_command" not in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=False))
    assert "run_command" in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=True))


def test_run_command_executes_in_project_cwd(workspace):
    _, a, _ = workspace
    # Approval off here so the unit test exercises execution directly (the gate
    # calls interrupt(), which needs a graph runtime — covered separately).
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out


def test_run_command_runs_via_shell(workspace):
    """run_command goes through the platform shell, so native shell operators work."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "echo one && echo two"}))
    # Exact lines (not substrings): the old argv path would print the literal "one && echo two",
    # so this assertion specifically fails unless the && actually chained two commands.
    assert [line.rstrip() for line in out.splitlines()] == ["one", "two"]


def test_run_command_declined_returns_not_raises(workspace, monkeypatch):
    """A declined approval RETURNS a plain result — NOT a ToolException. A decline is
    the operator's deliberate choice, not a failure: raising stamped status="error"
    and the chat rendered an undismissable full-bleed red block. The result names the
    declined command and tells the model not to retry. interrupt() is stubbed to deny."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "declined by the operator" in out
    assert _LIST_COMMAND in out
    assert "Do not re-run" in out


def test_run_command_bypass_skips_approval(workspace, monkeypatch):
    """Bypass-permissions mode (per-turn metadata + host allows): run_command runs WITHOUT the
    approval gate. interrupt() is stubbed to DENY, so if the gate were reached the command would
    raise — a clean run proves it was skipped."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
            filesystem_bypass_allowed=True,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out


def test_run_command_bypass_forbidden_by_host_still_gates(workspace, monkeypatch):
    """When the host forbids bypass (filesystem_bypass_allowed=False), caller bypass metadata is
    IGNORED and the approval gate still fires — here stubbed to deny, so the command doesn't run
    and returns the decline result (the gate firing is the point; the decline handling is
    covered by test_run_command_declined_returns_not_raises)."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
            filesystem_bypass_allowed=False,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "ls"}))
    assert "declined by the operator" in out  # gate fired despite the bypass request


# ── no_delete fence mode + delete_file (ADR 0083 D5, #2012) ────────────────────


def test_registry_parses_no_delete(workspace):
    """A project's `no_delete: true` config key lands on the Project model."""
    _, a, b = workspace
    from tools.fs_tools import _registry_from_config

    reg = _registry_from_config(
        _Cfg(
            filesystem_projects=[
                {"name": "a", "path": str(a), "write": True, "no_delete": True},
                {"name": "b", "path": str(b), "write": True},
            ]
        )
    )
    assert reg.get("a").no_delete is True
    assert reg.get("b").no_delete is False


def test_list_projects_reports_three_modes(workspace):
    """list_projects labels each mode: ro / rw / rw-no-delete."""
    tmp, a, b = workspace
    c = tmp / "projC"
    c.mkdir()
    t = _tools(
        _Cfg(
            filesystem_projects=[
                {"name": "rw", "path": str(a), "write": True},
                {"name": "ro", "path": str(b)},  # write defaults false
                {"name": "nod", "path": str(c), "write": True, "no_delete": True},
            ]
        )
    )
    out = t["list_projects"].invoke({})
    assert "rw  [rw]" in out
    assert "ro  [ro]" in out
    assert "nod  [rw/no-delete]" in out


def test_delete_file_present_and_refused_when_read_only(workspace):
    """delete_file is always built (it self-gates); a read-only project refuses it."""
    _, _, b = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "b", "path": str(b)}]))  # write:false
    assert "delete_file" in t
    out = t["delete_file"].invoke({"project": "b", "path": "notes.txt"})
    assert "read-only" in out
    assert (b / "notes.txt").exists()  # untouched


def test_delete_file_refused_in_no_delete_project(workspace):
    """A read-write-no-delete project refuses delete_file even though writes are allowed."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True, "no_delete": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "no_delete" in out
    assert (a / "README.md").exists()  # untouched


def test_delete_file_approved_removes(workspace, monkeypatch):
    """In a read-write project, an APPROVED delete removes the file. interrupt() → approve."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "approve")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert (a / "README.md").exists()
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "Deleted README.md" in out
    assert not (a / "README.md").exists()


def test_delete_file_declined_keeps_file(workspace, monkeypatch):
    """A DECLINED delete returns a plain decline (not a raise) and leaves the file."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "declined by the operator" in out
    assert "Do not retry" in out
    assert (a / "README.md").exists()  # kept


def test_delete_file_floor_not_bypassable(workspace, monkeypatch):
    """The permanent-delete floor ignores bypass-permissions: even with the /bypass toggle
    set (which skips run_command's gate), delete_file still asks — here stubbed to deny, so
    the file survives. This is the key difference from run_command."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a), "write": True}],
            filesystem_bypass_allowed=True,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "declined by the operator" in out  # gate fired despite bypass
    assert (a / "README.md").exists()  # floor held


def test_delete_file_refuses_directory(workspace, monkeypatch):
    """delete_file removes a single file, never a directory tree."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "approve")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "src"})
    assert "is a directory" in out
    assert (a / "src").is_dir()  # untouched


# ── config round-trip ──────────────────────────────────────────────────────────


def test_config_parses_filesystem(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text(
        "filesystem:\n  enabled: true\n  allow_run: true\n  projects:\n    - {name: orbis, path: /tmp, write: false}\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.filesystem_enabled is True
    assert cfg.filesystem_allow_run is True
    assert cfg.filesystem_projects[0]["name"] == "orbis"


def test_config_filesystem_default_on_fenced_workspace(tmp_path, monkeypatch):
    """Filesystem is ON by default (fenced to a workspace); run_command stays opt-in."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert cfg.filesystem_enabled is True
    # run_command is ON now (arbitrary argv, unsandboxed) but gated by HITL
    # approval by default — capable, not dangerous-by-default.
    assert cfg.filesystem_allow_run is True
    assert cfg.filesystem_run_requires_approval is True
    # No explicit projects → a single default `workspace` project, fenced + writable.
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "ws"))
    projects = cfg.effective_filesystem_projects(create=True)
    assert len(projects) == 1
    assert projects[0]["name"] == "workspace" and projects[0]["write"] is True
    assert (tmp_path / "ws").is_dir()  # created


def test_approved_accepts_known_shapes():
    from tools.fs_tools import _approved

    for yes in ("approve", "approved", "Yes", " OK ", True, {"approved": True}, {"decision": "approve"}):
        assert _approved(yes) is True, yes
    for no in ("deny", "denied", "no", "", False, {"approved": False}, {"decision": "deny"}, None):
        assert _approved(no) is False, no


def test_run_command_present_by_default_gated(tmp_path, monkeypatch):
    """Shell is on by default (allow_run) — run_command is built — and approval
    is required by default."""
    from graph.config import LangGraphConfig
    from tools.fs_tools import build_fs_tools

    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "ws"))
    cfg = LangGraphConfig()  # defaults: enabled + allow_run + requires_approval
    names = {getattr(t, "name", "") for t in build_fs_tools(cfg)}
    assert "run_command" in names


def test_effective_projects_explicit_wins_and_disabled_is_empty(tmp_path):
    from graph.config import LangGraphConfig

    explicit = [{"name": "repo", "path": str(tmp_path), "write": False}]
    cfg = LangGraphConfig(filesystem_projects=explicit)
    assert cfg.effective_filesystem_projects() == explicit  # explicit registry wins
    off = LangGraphConfig(filesystem_enabled=False)
    assert off.effective_filesystem_projects() == []  # disabled → no projects


# ── run_command shell grammars (#2518) ─────────────────────────────────────────


def test_shell_argv_default_grammars():
    from tools.fs_tools import _platform_shell_argv

    argv, runner = _platform_shell_argv("echo hi", windows=False)
    assert argv == ["/bin/sh", "-c", "echo hi"]
    assert runner == "/bin/sh -c"
    argv, runner = _platform_shell_argv("dir", windows=True)
    assert argv[0].lower().endswith("cmd.exe")
    assert argv[1:] == ["/d", "/s", "/c", "dir"]
    assert "cmd.exe" in runner.lower()


def test_shell_argv_powershell_is_encoded_and_unicode_safe():
    """PowerShell rides -EncodedCommand (Base64 UTF-16LE): the exact approved text
    crosses the process boundary with no re-quoting, and non-ASCII survives."""
    import base64

    from tools.fs_tools import _platform_shell_argv

    cmd = "Set-Content -LiteralPath 'PA Windows Tool [café] 日本語.txt' -Value 'café'"
    argv, runner = _platform_shell_argv(cmd, "powershell", windows=True)
    assert argv[0] == "powershell.exe"
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(argv[4]).decode("utf-16-le")
    assert decoded.endswith(cmd)  # byte-exact inner command, no escaping layer
    assert "OutputEncoding" in decoded  # UTF-8 stdout preamble
    assert "powershell.exe" in runner and "EncodedCommand" in runner
    argv, _ = _platform_shell_argv(cmd, "powershell", windows=False)
    assert argv[0] == "pwsh"


def test_shell_argv_rejects_unknown_and_cross_platform():
    from tools.fs_tools import _platform_shell_argv

    with pytest.raises(ValueError, match="unknown shell"):
        _platform_shell_argv("x", "fish")
    with pytest.raises(ValueError, match="Windows-only"):
        _platform_shell_argv("x", "cmd", windows=False)
    with pytest.raises(ValueError, match="not available on Windows"):
        _platform_shell_argv("x", "sh", windows=True)


def test_run_command_unknown_shell_returns_error(workspace):
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "ls", "shell": "fish"}))
    assert out.startswith("Error:") and "unknown shell" in out


def test_run_command_approval_detail_names_runner(workspace, monkeypatch):
    """The approval payload names the real executable chain, not just the inner
    command — approving PowerShell text that secretly ran under cmd.exe is the
    #2518 failure, so the operator must see the runner before approving."""
    import langgraph.types

    _, a, _ = workspace
    seen: dict = {}

    def fake_interrupt(payload):
        seen.update(payload)
        return "approve"

    monkeypatch.setattr(langgraph.types, "interrupt", fake_interrupt)
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out
    assert _LIST_COMMAND in seen["detail"]
    assert "runs via:" in seen["detail"]
    assert ("cmd.exe" in seen["detail"].lower()) or ("/bin/sh -c" in seen["detail"])


_PWSH = "powershell.exe" if os.name == "nt" else "pwsh"


@pytest.mark.skipif(shutil.which(_PWSH) is None, reason="PowerShell not installed")
def test_run_command_powershell_unicode_roundtrip(workspace):
    """#2518 acceptance: an ordinary Set-Content whose path and content carry
    spaces, brackets, accents, Japanese, a check mark, and a Greek letter works
    on the FIRST attempt — no [char] reconstruction, no cmd.exe reinterpretation."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a), "write": True}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    name = "PA Windows Tool [café] 日本語.txt"
    cmd = f"Set-Content -LiteralPath '{name}' -Value @('PA-WINDOWS-TOOL-OK','café','日本語 ✓ Ω') -Encoding utf8"
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": cmd, "shell": "powershell"}))
    assert not out.startswith("Error:"), out
    target = a / name
    assert target.exists()
    # utf-8-sig: Windows PowerShell 5.1 writes -Encoding utf8 WITH a BOM, pwsh without.
    text = target.read_bytes().decode("utf-8-sig")
    assert text.splitlines() == ["PA-WINDOWS-TOOL-OK", "café", "日本語 ✓ Ω"]


@pytest.mark.skipif(shutil.which(_PWSH) is None, reason="PowerShell not installed")
def test_run_command_powershell_stdout_unicode(workspace):
    """The UTF-8 output preamble holds: non-ASCII stdout decodes intact instead of
    arriving in the OEM code page as mojibake."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(
        t["run_command"].ainvoke({"project": "a", "command": "Write-Output 'café ✓ 日本語'", "shell": "powershell"})
    )
    assert "café ✓ 日本語" in out


# ── #2586: line endings survive the round trip, on every platform ─────────────


_UTF8_SAMPLE = "ASCII=PA-GEMMA-UTF8-OK\nLatin=café\nJapanese=日本語\nSymbols=✓ Ω\nFinal=line-5"


def test_write_file_keeps_lf_as_lf_on_disk(workspace):
    """The reported bug: on Windows, text mode rewrote every requested \\n as \\r\\n, so the
    file the agent asked for is not the file on disk. Asserted on BYTES — reading it back
    through Python's text mode is exactly the check that masked it."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    t["write_file"].invoke({"project": "a", "path": "lf.txt", "content": _UTF8_SAMPLE})

    raw = (a / "lf.txt").read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 4
    assert raw == _UTF8_SAMPLE.encode("utf-8")  # byte-exact, non-ASCII included
    assert not raw.endswith(b"\n")  # no trailing newline was added either


def test_write_file_preserves_crlf_when_that_is_what_was_asked_for(workspace):
    """Verbatim cuts both ways — content that wants CRLF still gets CRLF."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    t["write_file"].invoke({"project": "a", "path": "crlf.txt", "content": "one\r\ntwo\r\n"})

    assert (a / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n"


def test_read_file_does_not_mask_crlf(workspace):
    """The second half of the defect: read_file normalized \\r\\n back to \\n, so an agent
    comparing what it wrote to what it read got a false PASS."""
    _, a, _ = workspace
    (a / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    assert t["read_file"].invoke({"project": "a", "path": "win.txt"}) == "one\r\ntwo\r\n"


# ── read_file memoization (opt-in, tools.memoize_reads.enabled) ────────────────
# Found via a live-session transcript audit: the same file, read twice non-
# consecutively in one turn, returned byte-identical truncated content both times.

# write_text() applies platform newline translation (CRLF on Windows) — fine for
# the workspace fixture's src/main.py, which every OTHER test here only substring-
# checks, but wrong for these tests' exact `==` comparisons. write_bytes() with an
# explicit LF payload keeps the on-disk content (and therefore read_file's verbatim
# output) identical across platforms, matching read_file's own no-normalization
# contract (test_read_file_does_not_mask_crlf).
_EXACT_CONTENT = "print('hello')\nTODO: fix\n"


def _write_exact(a) -> str:
    (a / "exact.py").write_bytes(_EXACT_CONTENT.encode("utf-8"))
    return "exact.py"


def _msgs(*events):
    """Build a turn's message list from ("read"|"write"|"human", ...) shorthand.

    ("human",) -> HumanMessage marking the turn boundary
    ("read", id, project, path, offset, result) -> AIMessage(tool_calls=[...]) + ToolMessage
    ("write", id, project, path) -> AIMessage(tool_calls=[write_file]) + ToolMessage
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    out = []
    for ev in events:
        kind = ev[0]
        if kind == "human":
            out.append(HumanMessage(content="go"))
        elif kind == "read":
            _, cid, project, path, offset, result = ev
            args = {"project": project, "path": path, "offset": offset}
            out.append(AIMessage(content="", tool_calls=[{"name": "read_file", "args": args, "id": cid}]))
            out.append(ToolMessage(content=result, tool_call_id=cid, name="read_file"))
        elif kind == "write":
            _, cid, project, path = ev
            out.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "write_file", "args": {"project": project, "path": path, "content": "x"}, "id": cid}
                    ],
                )
            )
            out.append(ToolMessage(content="Overwrote.", tool_call_id=cid, name="write_file"))
    return out


def test_memoization_off_by_default_always_reads_for_real(workspace):
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    state = {"messages": _msgs(("human",), ("read", "1", "a", p, 1, "STALE CACHED CONTENT"))}
    out = t["read_file"].invoke({"project": "a", "path": p, "state": state})
    assert out == _EXACT_CONTENT  # the REAL file content, cache ignored


def test_memoization_serves_a_pointer_not_the_content_again(workspace):
    # The saving is real context tokens, not just a skipped disk read — a hit
    # returns a short pointer, not the (possibly huge) content a second time.
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {"messages": _msgs(("human",), ("read", "1", "a", p, 1, "CACHED CONTENT"))}
    out = t["read_file"].invoke({"project": "a", "path": p, "state": state})
    assert out != "CACHED CONTENT"
    assert "unchanged" in out and f"a/{p}" in out


def test_memoization_a_write_to_the_same_path_invalidates_the_cache(workspace):
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {
        "messages": _msgs(
            ("human",),
            ("read", "1", "a", p, 1, "STALE — before the write"),
            ("write", "2", "a", p),
        )
    }
    out = t["read_file"].invoke({"project": "a", "path": p, "state": state})
    assert out == _EXACT_CONTENT  # re-read for real, not the stale cache


def test_memoization_a_different_offset_is_not_treated_as_the_same_call(workspace):
    # Pagination and memoization must compose: reading a DIFFERENT chunk of the
    # same file is not a repeat, even though (project, path) matches.
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {"messages": _msgs(("human",), ("read", "1", "a", p, 1, "chunk at line 1"))}
    out = t["read_file"].invoke({"project": "a", "path": p, "offset": 2, "state": state})
    assert out != "chunk at line 1"
    assert out.startswith(_EXACT_CONTENT.splitlines(keepends=True)[1])  # the real file's second line onward


def test_memoization_a_different_limit_is_not_treated_as_the_same_call(workspace):
    # Same idea, the other axis: a wider/narrower window at the SAME offset is
    # still a different chunk, not a repeat.
    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {"messages": _msgs(("human",), ("read", "1", "a", p, 1, "stale, limit=1 worth of content"))}
    out = t["read_file"].invoke({"project": "a", "path": p, "offset": 1, "limit": 2, "state": state})
    assert out == _EXACT_CONTENT  # re-read for real — limit differs from the cached call's default


def test_memoization_an_unset_limit_and_an_explicit_default_sized_limit_are_not_the_same_call(workspace):
    # An UNSET limit and an explicit limit=_DEFAULT_READ_LINES both normalize to
    # the same numeric value, but they can return DIFFERENT content — unset
    # auto-extends past the default line count when the whole remainder still
    # fits the char cap; an explicit limit never does. The numeric value alone
    # is not a safe memoization key.
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from tools.fs_tools import _DEFAULT_READ_LINES

    _, a, _ = workspace
    n = _DEFAULT_READ_LINES + 500  # over the default line count, tiny in chars
    body = "".join(f"{i}\n" for i in range(n)).encode("utf-8")
    (a / "many.txt").write_bytes(body)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )

    # Cached: an EXPLICIT limit=_DEFAULT_READ_LINES call (truncated at exactly
    # that many lines) must not serve a later UNSET-limit call — that call would
    # for real return the auto-extended WHOLE file, strictly more content.
    explicit_cached_state = {
        "messages": [
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"project": "a", "path": "many.txt", "limit": _DEFAULT_READ_LINES},
                        "id": "1",
                    }
                ],
            ),
            ToolMessage(content="TRUNCATED — stands in for the real limited page", tool_call_id="1", name="read_file"),
        ]
    }
    unset_call = t["read_file"].invoke({"project": "a", "path": "many.txt", "state": explicit_cached_state})
    assert "TRUNCATED" not in unset_call  # NOT served the truncated cache as "unchanged"
    assert unset_call == body.decode("utf-8")  # real read — the full, auto-extended file

    # And the reverse: cached UNSET-limit call must not serve a later EXPLICIT
    # limit=_DEFAULT_READ_LINES call — that call must actually truncate.
    unset_cached_state = {
        "messages": [
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"project": "a", "path": "many.txt"}, "id": "1"}],
            ),
            ToolMessage(
                content="WHOLE_FILE — stands in for the real auto-extended page", tool_call_id="1", name="read_file"
            ),
        ]
    }
    explicit_call = t["read_file"].invoke(
        {"project": "a", "path": "many.txt", "limit": _DEFAULT_READ_LINES, "state": unset_cached_state}
    )
    assert "WHOLE_FILE" not in explicit_call  # NOT served the unset-limit cache as "unchanged"
    assert "call again" in explicit_call  # real read — genuinely truncated at the explicit limit


def test_memoization_normalizes_the_cached_calls_pagination_the_same_way_read_file_does(workspace):
    # A cached call's explicit offset=0/limit=0 must compare as read_file itself
    # would clamp them (max(1, v)) — NOT jump to the unrelated defaults via a
    # naive `v or default` fallback, which would (wrongly) treat limit=0 as if
    # it read 1000 lines instead of the 1 it actually reads.
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {
        "messages": [
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"project": "a", "path": p, "offset": 0, "limit": 0}, "id": "1"}
                ],
            ),
            ToolMessage(content="cached at the clamped (1, 1)", tool_call_id="1", name="read_file"),
        ]
    }
    # A fresh call with the SAME effective (offset=1, limit=1) the cached 0/0 call
    # actually clamped to — must hit the cache.
    hit = t["read_file"].invoke({"project": "a", "path": p, "offset": 1, "limit": 1, "state": state})
    assert "unchanged" in hit

    # A fresh call at the DEFAULT limit (1000) is a genuinely different chunk —
    # must NOT hit the cache just because 0 is falsy.
    miss = t["read_file"].invoke({"project": "a", "path": p, "offset": 1, "state": state})
    assert miss == _EXACT_CONTENT


def test_memoization_a_different_path_is_not_confused_with_the_cached_one(workspace):
    _, a, _ = workspace
    p = _write_exact(a)
    (a / "other.py").write_bytes(b"different file")
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {"messages": _msgs(("human",), ("read", "1", "a", p, 1, "cached content"))}
    out = t["read_file"].invoke({"project": "a", "path": "other.py", "state": state})
    assert out == "different file"


def test_memoization_only_scans_the_current_turn(workspace):
    # A read from a PRIOR turn must not be treated as still valid this turn.
    from langchain_core.messages import HumanMessage

    _, a, _ = workspace
    p = _write_exact(a)
    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {
        "messages": [
            *_msgs(("human",), ("read", "1", "a", p, 1, "turn 1's cached content")),
            HumanMessage(content="turn 2"),
        ]
    }
    out = t["read_file"].invoke({"project": "a", "path": p, "state": state})
    assert out == _EXACT_CONTENT  # re-read — the cached read belongs to turn 1


def test_memoization_an_errored_prior_read_is_not_served_as_a_cached_success(workspace):
    _, a, _ = workspace
    p = _write_exact(a)
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    t = _tools(
        _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}], tools_memoize_reads_enabled=True)
    )
    state = {
        "messages": [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"project": "a", "path": p}, "id": "1"}]),
            ToolMessage(content="Error: cannot read", tool_call_id="1", name="read_file", status="error"),
        ]
    }
    out = t["read_file"].invoke({"project": "a", "path": p, "state": state})
    assert out == _EXACT_CONTENT  # a real read, not the errored one echoed back


def test_write_then_read_round_trips_exactly(workspace):
    """What the agent's own verification actually depends on."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    t["write_file"].invoke({"project": "a", "path": "rt.txt", "content": _UTF8_SAMPLE})

    assert t["read_file"].invoke({"project": "a", "path": "rt.txt"}) == _UTF8_SAMPLE


def test_edit_file_does_not_convert_a_crlf_file_to_lf(workspace):
    """An LF needle against a CRLF file must still match — and must not rewrite the file's
    other line endings as a side effect of a one-line edit."""
    _, a, _ = workspace
    (a / "win.txt").write_bytes(b"alpha\r\nbravo\r\ncharlie\r\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["edit_file"].invoke({"project": "a", "path": "win.txt", "old": "bravo\ncharlie", "new": "bravo\ndelta"})

    assert "Edited" in out
    assert (a / "win.txt").read_bytes() == b"alpha\r\nbravo\r\ndelta\r\n"  # CRLF throughout


def test_edit_file_keeps_lf_file_as_lf(workspace):
    _, a, _ = workspace
    (a / "unix.txt").write_bytes(b"alpha\nbravo\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    t["edit_file"].invoke({"project": "a", "path": "unix.txt", "old": "bravo", "new": "charlie"})

    assert (a / "unix.txt").read_bytes() == b"alpha\ncharlie\n"


def test_edit_file_still_rejects_an_ambiguous_needle_in_a_crlf_file(workspace):
    """The uniqueness guard must count matches in the CONVERTED needle, not the original —
    otherwise a CRLF file silently loses the ambiguity check."""
    _, a, _ = workspace
    (a / "dup.txt").write_bytes(b"x\r\ny\r\nx\r\ny\r\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    out = t["edit_file"].invoke({"project": "a", "path": "dup.txt", "old": "x\ny", "new": "z"})

    assert out.startswith("Error:") and "not unique" in out


def test_the_newline_translation_that_bit_windows_is_disabled(tmp_path):
    """The write-side defect is invisible on this platform, so reproduce the mechanism
    explicitly rather than trusting a green test on macOS/Linux.

    Text mode with the default ``newline=None`` writes ``os.linesep`` for every ``\\n`` — which
    IS ``\\r\\n`` on Windows and ``\\n`` here. Opening with ``newline="\\r\\n"`` performs that
    same translation on any platform, so the first half below is the reported corruption,
    reproduced locally; the second half is the fix.
    """
    from tools.fs_tools import _write_text_verbatim

    p = tmp_path / "sim.txt"
    with p.open("w", encoding="utf-8", newline="\r\n") as f:  # what Windows' default does
        f.write("a\nb\n")
    assert p.read_bytes() == b"a\r\nb\r\n"  # ← the bug

    _write_text_verbatim(p, "a\nb\n")
    assert p.read_bytes() == b"a\nb\n"  # ← the fix, same platform, same call shape


# ── live registry lookup: mid-turn registrations are visible same-turn (#2836) ─


def _wire_live_config(monkeypatch, initial):
    """Wire ``HOST.config`` the way the server does (a getter for the LIVE config)
    and return a setter that swaps in a replacement — what ``apply_settings``'
    reload does to ``STATE.graph_config`` while the old tool closures keep running."""
    from graph.plugins.host import HOST

    state = {"cfg": initial}
    monkeypatch.setattr(HOST, "config", lambda: state["cfg"])

    def swap(cfg):
        state["cfg"] = cfg

    return swap


def test_project_registered_mid_turn_is_visible_same_turn(workspace, monkeypatch):
    """The #2836 fix: the tools are built ONCE (long-lived closures), then the live
    config gains a project — the way onboard_project's apply_settings lands mid-turn.
    Every fs tool must see it on its very next call, without a graph rebuild."""
    _, a, b = workspace
    entry_a = {"name": "a", "path": str(a), "write": True}
    swap = _wire_live_config(monkeypatch, _Cfg(filesystem_projects=[entry_a]))
    t = _tools(_Cfg(filesystem_projects=[entry_a]))

    assert "projB" not in t["list_projects"].invoke({})
    assert "unknown project" in t["list_dir"].invoke({"project": "b", "path": "."})

    # onboard_project succeeded: the live config now carries b. Same closures.
    swap(_Cfg(filesystem_projects=[entry_a, {"name": "b", "path": str(b)}]))

    listed = t["list_projects"].invoke({})
    assert "- b " in listed and str(b) in listed
    assert "notes.txt" in t["list_dir"].invoke({"project": "b", "path": "."})
    assert "read only" in t["read_file"].invoke({"project": "b", "path": "notes.txt"})
    assert "notes.txt" in t["find_files"].invoke({"project": "b", "pattern": "**/*.txt"})
    assert "notes.txt" in t["search_files"].invoke({"project": "b", "query": "read only"})

    # The refreshed registry is a real fence, not just a name list: the new
    # project keeps its mode (registered read-only) and its path containment.
    assert "read-only" in t["write_file"].invoke({"project": "b", "path": "x.txt", "content": "hi"})
    assert "escapes" in t["read_file"].invoke({"project": "b", "path": "../projA/README.md"})
    # ... and the pre-registered project is untouched by the refresh.
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})

    # Symmetric: a project dropped from the live config disappears immediately too.
    swap(_Cfg(filesystem_projects=[entry_a]))
    assert "unknown project" in t["list_dir"].invoke({"project": "b", "path": "."})


def test_unwired_or_broken_host_falls_back_to_build_config(workspace, monkeypatch):
    """No server (``HOST.config`` unwired) → the build-time config is the registry
    source, exactly the old snapshot behavior. A getter that RAISES (a mid-reload
    race) must degrade the same way — the live seam never breaks a tool call."""
    from graph.plugins.host import HOST

    _, a, _ = workspace
    cfg = _Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}])

    monkeypatch.setattr(HOST, "config", None)  # headless/test: the seam is not wired
    t = _tools(cfg)
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})

    def _boom():
        raise RuntimeError("config store mid-reload")

    monkeypatch.setattr(HOST, "config", _boom)  # wired but broken: fall back, don't raise
    assert "- a " in t["list_projects"].invoke({})
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})
    assert "README.md" in t["list_dir"].invoke({"project": "a", "path": "."})


def test_live_getter_returning_none_falls_back_to_build_config(workspace, monkeypatch):
    """A wired getter that returns ``None`` (server mid-swap) is the third seam
    failure shape — same fallback, same snapshot behavior."""
    from graph.plugins.host import HOST

    _, a, _ = workspace
    monkeypatch.setattr(HOST, "config", lambda: None)
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))

    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})
