"""The round-trip YAML parser is shared across threads — loads must not trample each other.

ruamel's ``YAML`` keeps its reader/scanner state on the instance, so one instance
parsing on two threads at once corrupts both parses (``IndexError: string index out of
range`` deep in ``ruamel.yaml.reader``). Config reads happen on the event loop while a
config write loads the same file in a worker thread (e.g. two ``POST /api/delegates``
at once), so this is a live path, not a hypothetical.
"""

from __future__ import annotations

import threading

import pytest

from graph import config_io

pytestmark = pytest.mark.skipif(not config_io._HAS_RUAMEL, reason="ruamel.yaml not installed")


def _doc(n: int) -> str:
    lines = ["# a comment to keep", "delegates:"]
    for i in range(n):
        lines += [f"  - name: coder{i}", "    type: acp", f"    command: 'echo {i}'", f"    workdir: /tmp/w{i}"]
    return "\n".join(lines) + "\n"


def test_concurrent_loads_and_dumps_from_many_threads_all_parse(tmp_path):
    path = tmp_path / "langgraph-config.yaml"
    path.write_text(_doc(120), encoding="utf-8")
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def _worker(k: int) -> None:
        try:
            start.wait()
            for _ in range(4):
                doc = config_io.load_yaml_doc(path)
                assert len(doc["delegates"]) == 120
                if k % 2:
                    config_io.save_yaml_doc(doc, tmp_path / f"out{k}.yaml")
        except BaseException as e:  # noqa: BLE001 — collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"{len(errors)} thread(s) failed; first: {errors[0]!r}"
    # The dump still round-trips comments + layout (the per-thread parser is configured
    # like the old shared one).
    out = (tmp_path / "out1.yaml").read_text(encoding="utf-8")
    assert out.startswith("# a comment to keep\n") and "  - name: coder0\n" in out
