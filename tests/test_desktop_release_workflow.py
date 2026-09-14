from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-build.yml"
MARKETING_WORKFLOW = ROOT / ".github" / "workflows" / "marketing-deploy.yml"


def test_tagged_desktop_build_refreshes_marketing_after_assets_are_live() -> None:
    """The public download page advances only after a complete desktop release."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["refresh-marketing"]

    assert set(job["needs"]) == {"build", "updater-manifest"}

    condition = " ".join(job["if"].split())
    assert condition == (
        "always() && "
        "inputs.tag != '' && "
        "github.repository == 'protoLabsAI/protoAgent' && "
        "needs.build.result == 'success' && "
        "needs.updater-manifest.result == 'success'"
    )

    assert job["permissions"] == {"contents": "read"}
    assert job["uses"] == "./.github/workflows/marketing-deploy.yml"
    assert job["secrets"] == "inherit"
    assert "runs-on" not in job
    assert "steps" not in job

    marketing = yaml.safe_load(MARKETING_WORKFLOW.read_text(encoding="utf-8"))
    triggers = marketing.get("on") or marketing.get(True)
    assert "workflow_call" in triggers


def test_every_desktop_leg_runs_the_frozen_fleet_deck() -> None:
    """#3498: the frozen sidecar opens the fleet deck (`protoagent-server fleet`, #3473), but
    the leg's live smoke only boots the server. Every leg must run the deck smoke against the
    same frozen binary, after the freeze and before packaging, and fail the leg on error."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["build"]
    targets = {leg["target"] for leg in job["strategy"]["matrix"]["include"]}
    assert targets == {"aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"}

    steps = job["steps"]
    runs = [step.get("run", "") for step in steps]
    freeze = next(i for i, run in enumerate(runs) if "build_sidecar.py" in run)
    smoke = next(i for i, run in enumerate(runs) if "scripts/fleet_deck_smoke.py --bin" in run)
    package = [step.get("name") for step in steps].index("Build + sign the app")
    assert freeze < smoke < package

    step = steps[smoke]
    assert "if" not in step, "the deck smoke must run on every leg, not one platform"
    assert not step.get("continue-on-error")
    assert 'BIN="apps/desktop/src-tauri/binaries/protoagent-server-${{ matrix.target }}"' in step["run"]
