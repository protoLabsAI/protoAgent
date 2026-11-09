"""The DS plugin-kit (#4805) exposes the event-bus helpers the console's plugin views adopt.

protoContent card bd-35j9e added `subscribe`, `publish`, `onEvent`, `getState` and `setState`
to @protolabsai/ui's no-build plugin-kit; this card bumps `apps/web` to the release that
carries them (0.71.0), and `apps/web/scripts/copy-plugin-kit.mjs` copies that file verbatim
into `public/_ds/plugin-kit.js` at build time so plugin iframes load it same-origin.

The kit is an npm artifact and is NOT committed. The dependency pin is checked in
Python CI; apps/web/src/lib/pluginKitBus.test.ts checks the copied artifact's exports
and runs the actual kit against the host relay in the web unit CI lane (without skips).
The Python artifact check below is an additional check when a web build is available.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "apps" / "web"

# The @protolabsai/ui release that first shipped the kit bus helpers (bd-35j9e).
_MIN_UI = (0, 71, 0)
_BUS_HELPERS = ("subscribe", "publish", "onEvent")


def _dep(name: str) -> str:
    deps = json.loads((WEB / "package.json").read_text(encoding="utf-8"))["dependencies"]
    return deps[name]


def _version(spec: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", spec)
    assert m, f"unparseable version range: {spec!r}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_web_pins_the_ui_release_that_carries_the_bus_helpers():
    """Both ui and ui-css are bumped to (at least) the release that exports the bus helpers."""
    for name in ("@protolabsai/ui", "@protolabsai/ui-css"):
        spec = _dep(name)
        assert _version(spec) >= _MIN_UI, (
            f"{name} is {spec!r}; needs >= {'.'.join(map(str, _MIN_UI))} for kit.subscribe/publish/onEvent"
        )


def _kit_js() -> Path | None:
    """The copied kit if a build ran, else the installed source it is copied from. Scoped to
    apps/web (and its own node_modules), not the parent checkout, which can hold a stale copy."""
    for candidate in (
        WEB / "public" / "_ds" / "plugin-kit.js",
        WEB / "dist" / "_ds" / "plugin-kit.js",
        WEB / "node_modules" / "@protolabsai" / "ui" / "src" / "plugin-kit.js",
    ):
        if candidate.is_file():
            return candidate
    return None


def test_the_copied_kit_exports_subscribe_publish_and_onevent():
    """Where the real kit resolves (built into public/_ds or `npm ci`-installed), the file the
    copy script lands exports the bus helpers the org chart view now calls."""
    kit = _kit_js()
    if kit is None:
        pytest.skip("plugin-kit.js not resolvable (needs an apps/web build or `npm ci`)")
    src = kit.read_text(encoding="utf-8")
    missing = [h for h in _BUS_HELPERS if not re.search(rf"export\s+(?:async\s+)?function\s+{h}\b", src)]
    assert not missing, f"{kit} is missing kit bus helper export(s): {missing}"
