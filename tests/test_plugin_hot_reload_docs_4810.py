"""Plugin hot-reload docs must match the code (#4810 drift sweep, part 1).

Three plugin docs described router/surface hot-reload wrong, and a wrong doc about an
extension contract is worse than none — a plugin author codes against it. These are
docs-as-code drift guards: they read the committed files by PATH (not via ``import``, so
they pin the worktree's own source regardless of how the test venv resolves ``graph``)
and fail if a corrected claim is reverted or the debunked one creeps back.

The generated reference page is kept in sync separately by
``test_plugin_api_reference`` — here we only assert the ``live_config`` docstring (its
source of truth) carries the corrected reason and not the wrong one.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "plugins" / "plugin-devkit" / "skills" / "building-plugins" / "SKILL.md"
REGISTRY = REPO / "src" / "graph" / "plugins" / "registry.py"
PLUGINS_GUIDE = REPO / "docs" / "guides" / "plugins.md"
REGISTRY_REF = REPO / "docs" / "reference" / "plugin-registry-api.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── r1: SKILL.md router remount ──────────────────────────────────────────────


def test_skill_md_does_not_claim_disabling_leaves_a_stale_route() -> None:
    text = _read(SKILL)
    # The debunked claims (_mount_plugin_routers removes a disabled plugin's routes).
    assert "FastAPI can't unmount" not in text
    assert "leaves a stale route behind" not in text
    assert "that path\n  alone wants a restart" not in text


def test_skill_md_cites_mount_plugin_routers_for_live_remount() -> None:
    text = _read(SKILL)
    assert "Enabling, disabling and reloading are all live for routes" in text
    assert "_mount_plugin_routers" in text
    assert "plugin_wiring.py" in text
    # The one real caveat is kept: a request during a swap can use the old route, never a 404.
    assert "never a 404" in text


# ── r2: registry.live_config docstring (source of the generated reference) ───


def _live_config_docstring() -> str:
    """The ``live_config`` docstring read straight from the source file — robust against a
    test venv whose editable install resolves ``graph`` to a different checkout."""
    src = _read(REGISTRY)
    anchor = "def live_config(self)"
    assert anchor in src, "live_config moved or was renamed — update this guard"
    body = src[src.index(anchor) :]
    start = body.index('"""')
    end = body.index('"""', start + 3)
    return body[start + 3 : end]


def test_live_config_docstring_drops_the_wrong_reason() -> None:
    doc = _live_config_docstring()
    assert "FastAPI can't re-mount a router" not in doc
    assert "can't re-mount" not in doc


def test_live_config_docstring_states_the_real_reason() -> None:
    doc = _live_config_docstring()
    # The real reason: self.config is bound once per registry instance; a long-lived
    # handler/surface keeps that snapshot, while live_config re-reads STATE.graph_config.
    assert "config snapshot supplied when this registry is created" in doc
    assert "calls ``register()`` again" in doc
    assert "STATE.graph_config" in doc


def test_generated_registry_reference_reflects_the_corrected_docstring() -> None:
    ref = _read(REGISTRY_REF)
    assert "config snapshot supplied when this registry is created" in ref
    assert "FastAPI can't re-mount a router" not in ref


# ── r3: plugins.md teardown line ─────────────────────────────────────────────


def test_plugins_guide_has_no_nonexistent_teardown_api() -> None:
    text = _read(PLUGINS_GUIDE)
    assert "register teardown so nothing lingers" not in text


def test_plugins_guide_describes_on_subscription_ownership() -> None:
    text = _read(PLUGINS_GUIDE)
    assert "ownership follows where you call `registry.on`" in text
    assert "(#4885)" in text
    # register()-scoped on() is registry-owned; post-load on() hands the caller an
    # unsubscribe to call from stop().
    assert "bundle-owned" in text
    assert "`stop()`" in text


# ── r4: payload validation claim is accurate and must stay put ───────────────


def test_plugins_guide_still_says_payloads_are_not_validated() -> None:
    # Verified against the emit/publish path (registry.emit → host.publish → EventBus):
    # neither consults emits_schemas, so the existing text is correct and left unchanged.
    text = _read(PLUGINS_GUIDE)
    assert "Nothing validates payloads at publish time." in text
