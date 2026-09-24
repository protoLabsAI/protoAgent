"""The desktop `build` skips cleanly when the server sidecar is absent.

`apps/desktop` builds via `tauri build`, which bundles the PyInstaller server sidecar as
an externalBin. Only the Desktop Build workflow freezes that sidecar first; a plain
`npm run --workspaces build` (fresh clone, the console CI sweep, any contributor machine)
has no sidecar, and a bare `tauri build` HARD-FAILS there with
"resource path `binaries/protoagent-server-...` doesn't exist" — taking the whole
workspace build down with it.

`apps/desktop/scripts/build-if-sidecar.mjs` guards it: skip (exit 0) with no sidecar, run
the real `tauri build` (forwarding every arg) once one exists. These tests pin BOTH paths
so the guard can't silently regress into a bare `tauri build` (or stop forwarding the
release's `-- --bundles ... --config '{json}'`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "apps" / "desktop"
GUARD = DESKTOP / "scripts" / "build-if-sidecar.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed (CI runners carry it)")


def test_package_json_build_delegates_to_the_guard() -> None:
    """The `build` script must run the guard, not a bare `tauri build` — otherwise the
    workspace sweep is back to hard-failing without a sidecar."""
    pkg = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["build"] == "node scripts/build-if-sidecar.mjs"
    # `dev` stays a direct tauri invocation (a developer running it wants the real thing).
    assert pkg["scripts"]["dev"] == "tauri dev"
    assert GUARD.is_file()


def _stage(tmp_path: Path) -> Path:
    """Copy the guard into a throwaway tree mirroring apps/desktop/scripts/, so it resolves
    `../src-tauri/binaries` and walks up for node_modules exactly as it does in the repo."""
    scripts = tmp_path / "apps" / "desktop" / "scripts"
    scripts.mkdir(parents=True)
    staged = scripts / GUARD.name
    staged.write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
    return staged


def test_skips_with_exit_zero_when_no_sidecar(tmp_path: Path) -> None:
    """The bug every prior attempt hit: no sidecar present must NOT fail the build."""
    staged = _stage(tmp_path)
    proc = subprocess.run(
        [NODE, str(staged), "--bundles", "dmg"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Skipping `tauri build`" in proc.stdout


def test_runs_tauri_and_forwards_args_when_sidecar_present(tmp_path: Path) -> None:
    """With a sidecar present the guard must invoke the tauri CLI, forwarding every arg
    verbatim — including the release's JSON `--config`, which must arrive as ONE argument."""
    staged = _stage(tmp_path)
    desktop = staged.parent.parent  # tmp/apps/desktop

    # A stand-in sidecar so the guard takes the build path.
    binaries = desktop / "src-tauri" / "binaries"
    binaries.mkdir(parents=True)
    (binaries / "protoagent-server-aarch64-apple-darwin").write_text("", encoding="utf-8")

    # A stub @tauri-apps/cli whose bin echoes its argv instead of building.
    cli = desktop / "node_modules" / "@tauri-apps" / "cli"
    cli.mkdir(parents=True)
    (cli / "package.json").write_text(json.dumps({"name": "@tauri-apps/cli", "bin": {"tauri": "stub.js"}}))
    (cli / "stub.js").write_text('console.log("ARGV=" + JSON.stringify(process.argv.slice(2)));')

    config_arg = '{"bundle":{"createUpdaterArtifacts":"v1Compatible"}}'
    proc = subprocess.run(
        [NODE, str(staged), "--bundles", "app,dmg", "--config", config_arg],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Skipping" not in proc.stdout
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("ARGV="))
    argv = json.loads(line[len("ARGV=") :])
    assert argv == ["build", "--bundles", "app,dmg", "--config", config_arg]
