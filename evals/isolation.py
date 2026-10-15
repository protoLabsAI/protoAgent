"""CLI-scoped temporary roots for synthetic evaluations, never production stores."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from infra.paths import instance_paths, reset_instance_paths


@contextmanager
def isolated_instance(prefix: str, soul_text: str):
    """Restore the environment and cached paths even when seeding or evaluation fails.

    Environment mutation is process-wide: use only for a standalone CLI, not
    concurrent experiments in a running server.
    """
    previous = {k: os.environ.get(k) for k in ("PROTOAGENT_BOX_ROOT", "PROTOAGENT_HOME")}
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as root:
            os.environ.update(PROTOAGENT_BOX_ROOT=root, PROTOAGENT_HOME=root)
            reset_instance_paths()
            soul = instance_paths().soul_path
            soul.parent.mkdir(parents=True, exist_ok=True)
            soul.write_text(soul_text, encoding="utf-8")
            yield Path(root)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_instance_paths()


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
