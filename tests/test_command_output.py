"""tools.command_output — compaction of long command output (#5784, slice 1)."""

from __future__ import annotations

import re
from pathlib import Path

from tools.command_output import SPILL_MAX_BYTES, compact_output, write_spill

BIG = 10_000_000


def test_strips_ansi_colour_cursor_and_osc_sequences():
    text = (
        "\x1b[1;32mPASSED\x1b[0m tests/test_a.py\n"
        "\x1b[2K\x1b[1Gcollecting\x1b[?25l\x1b[3A\n"
        "\x1b]0;window title\x07\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\\n"
        "\x1b(B\x1b7done\x1b8\n"
    )
    out = compact_output(text, threshold=BIG)
    assert "\x1b" not in out
    assert "\x07" not in out
    assert out.splitlines() == ["PASSED tests/test_a.py", "collecting", "link", "done"]


def test_carriage_return_redraw_keeps_final_state():
    assert compact_output("10%\r50%\r100%\n", threshold=BIG).splitlines() == ["100%"]


def test_crlf_is_a_plain_newline():
    assert compact_output("a\r\nb\r\n", threshold=BIG).splitlines() == ["a", "b"]


def test_identical_lines_collapse_with_count():
    text = "start\n" + "warning: deprecated\n" * 50 + "end\n"
    assert compact_output(text, threshold=BIG).splitlines() == [
        "start",
        "warning: deprecated (repeated 50 times)",
        "end",
    ]


def test_lines_differing_only_in_numbers_collapse_with_count():
    text = "".join(f"Downloading chunk {i}/50 ({i * 2}%)\n" for i in range(1, 51))
    lines = compact_output(text, threshold=BIG).splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("(repeated 50 times)")
    assert lines[0].startswith("Downloading chunk 50/50 (100%)")


def test_distinct_lines_are_not_collapsed():
    text = "tests/test_a.py PASSED\ntests/test_b.py PASSED\n"
    assert compact_output(text, threshold=BIG).splitlines() == [
        "tests/test_a.py PASSED",
        "tests/test_b.py PASSED",
    ]


def test_under_threshold_returns_compacted_text_without_marker():
    out = compact_output("one\ntwo\n", threshold=100, spill_path="/tmp/x.txt")
    assert out == "one\ntwo"
    assert "omitted" not in out


def test_over_threshold_keeps_head_and_tail_with_one_exact_marker():
    lines = [f"line-{chr(97 + i % 26)}{'x' * (i % 7)}-{'abcdefgh'[i % 8]}" for i in range(400)]
    # Make every line distinct by content (not just digits) so nothing collapses.
    lines = [f"{ln}-{'qwertyuiopasdfghjklzxcvbnm'[i % 26]}{'mnbvcxz'[i % 7]}" for i, ln in enumerate(lines)]
    lines[0] = "FIRST LINE"
    lines[-1] = "LAST LINE"
    text = "\n".join(lines) + "\n"
    collapsed = compact_output(text, threshold=BIG).splitlines()
    total = len(collapsed)

    spill = "/data/command-output/cmd-1.txt"
    threshold = 500
    out = compact_output(text, threshold=threshold, spill_path=spill)
    out_lines = out.splitlines()
    assert out_lines[0] == "FIRST LINE"
    assert out_lines[-1] == "LAST LINE"

    markers = [ln for ln in out_lines if "lines omitted" in ln]
    assert len(markers) == 1
    marker = markers[0]
    kept = len(out_lines) - 1
    dropped = total - kept
    assert dropped > 0
    assert re.search(rf"\b{dropped} of {total} lines omitted\b", marker)
    assert spill in marker
    # Everything except the marker fits the threshold.
    assert len(out) - len(marker) <= threshold


def test_marker_without_spill_path_names_no_path():
    text = "\n".join(f"row {chr(65 + i % 26)}{chr(97 + i // 26 % 26)}" for i in range(300))
    out = compact_output(text, threshold=200)
    marker = [ln for ln in out.splitlines() if "lines omitted" in ln]
    assert len(marker) == 1
    assert "saved to" not in marker[0]


def test_single_giant_line_is_bounded():
    out = compact_output("y" * 100_000, threshold=1000, spill_path="/s.txt")
    marker = next(ln for ln in out.splitlines() if "lines omitted" in ln)
    assert len(out) - len(marker) <= 1000
    assert "/s.txt" in marker


def test_compact_is_deterministic():
    text = "\x1b[31ma\x1b[0m\r\n" * 3 + "b\n" * 2000
    assert compact_output(text, threshold=300, spill_path="p") == compact_output(text, threshold=300, spill_path="p")


def test_spill_writes_text_byte_for_byte_and_paths_are_unique(tmp_path: Path):
    text = "héllo \x1b[1mworld\x1b[0m\r\n50%\r100%\n" * 10
    p1 = write_spill(text, root=tmp_path)
    p2 = write_spill(text, root=tmp_path)
    assert p1 != p2
    for p in (p1, p2):
        path = Path(p)
        assert path.is_absolute()
        assert path.parent == tmp_path.resolve()
        assert path.read_bytes() == text.encode("utf-8")


def test_spill_defaults_to_instance_data_root(tmp_path: Path, monkeypatch):
    import tools.command_output as co

    monkeypatch.setattr(co, "_spill_dir", lambda: tmp_path / "command-output")
    p = write_spill("full output")
    assert Path(p).parent == (tmp_path / "command-output").resolve()
    assert Path(p).read_text(encoding="utf-8") == "full output"


def test_spill_is_capped_with_a_note(tmp_path: Path, monkeypatch):
    import tools.command_output as co

    monkeypatch.setattr(co, "SPILL_MAX_BYTES", 16)
    data = Path(write_spill("a" * 100, root=tmp_path)).read_bytes()
    assert data.startswith(b"a" * 16)
    assert b"a" * 17 not in data
    assert b"spill capped at 16 bytes" in data
    assert SPILL_MAX_BYTES == 5 * 1024 * 1024
