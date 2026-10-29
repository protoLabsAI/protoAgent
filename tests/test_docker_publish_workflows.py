"""Both Docker publishing paths must support native Intel and ARM installs."""

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("filename", "job"),
    [("docker-publish.yml", "build-and-push"), ("release.yml", "release")],
)
def test_docker_publish_platforms_and_emulation(filename, job):
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
    )
    steps = workflow["jobs"][job]["steps"]

    def action_index(action):
        return next(
            i for i, step in enumerate(steps)
            if step.get("uses", "").startswith(f"docker/{action}@")
        )

    qemu = action_index("setup-qemu-action")
    buildx = action_index("setup-buildx-action")
    build = action_index("build-push-action")
    assert qemu < buildx < build
    assert re.fullmatch(r"docker/setup-qemu-action@v\d+", steps[qemu]["uses"])
    assert steps[build]["with"]["platforms"].split(",") == [
        "linux/amd64", "linux/arm64",
    ]
    assert steps[build]["with"]["push"] is True
