"""Plugin-view docs must match the code (#4810 drift sweep, part 2).

Part 2 corrects the hidden-delivery (`background: true`) re-open claim, adds a Heavy
views (WebGL/WASM) section, and documents the ``multimodal_tool_result`` tool-return
recipe. A wrong doc about an extension contract is worse than none — a plugin author
codes against it — so these are docs-as-code drift guards: they read the committed files
by PATH (not via ``import``, so they pin the worktree's own source regardless of how the
test venv resolves ``graph``) and fail if a corrected claim is reverted, the overbroad
one creeps back, or a quoted limit drifts from the code it was read off of.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VIEWS_GUIDE = REPO / "docs" / "guides" / "building-react-plugin-views.md"
PLUGINS_GUIDE = REPO / "docs" / "guides" / "plugins.md"
MULTIMODAL = REPO / "src" / "graph" / "multimodal.py"
SDK = REPO / "src" / "graph" / "sdk.py"
PLUGIN_VIEW_TSX = REPO / "apps" / "web" / "src" / "app" / "PluginView.tsx"
UI_STORE = REPO / "apps" / "web" / "src" / "state" / "uiStore.ts"
APP_TSX = REPO / "apps" / "web" / "src" / "app" / "App.tsx"
SKILL = REPO / "plugins" / "plugin-devkit" / "skills" / "building-plugins" / "SKILL.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── r1: background re-open text matches App.tsx / uiStore.ts / PluginView.tsx ────


def test_views_guide_drops_the_overbroad_reopen_claim() -> None:
    text = _read(VIEWS_GUIDE)
    # The debunked unqualified promise. True only within one console tab; a full reload
    # tears the iframe down (pluginBackground is partialized out of the UI store).
    assert "reloaded on re-open" not in text


def test_views_guide_qualifies_reopen_to_the_tab_and_names_remount_cases() -> None:
    text = _read(VIEWS_GUIDE)
    assert "Re-open is only free within the same console tab." in text
    # The within-tab guarantee cites the fix that keeps the iframe mounted on a swap.
    assert "the fix in #4802" in text
    # Case 1 — a full console reload forgets the session-scoped, unpersisted opt-in.
    assert "A full console reload" in text
    assert "session-scoped and never persisted" in text
    assert "re-send `background: true` on every load" in text
    # Case 2 — kept warm only in a dock the console renders; one mounted copy across docks.
    assert "No dock, or a second dock" in text


def test_ui_store_really_partializes_out_plugin_background() -> None:
    # Source of the "session-scoped, never persisted" claim: pluginBackground is dropped
    # from the persisted UI state by `partialize`, so a reload forgets it.
    text = _read(UI_STORE)
    assert "pluginBackground: _pb" in text
    assert "partialize" in text


def test_app_keeps_one_display_swapped_copy_per_background_view() -> None:
    # Source of the within-tab "not a remount" + "one copy across docks" claims.
    text = _read(APP_TSX)
    assert "plugin-bg-slot" in text
    assert "(#4802)" in text
    assert "is open in another panel" in text


# ── r2: Heavy views (WebGL/WASM) section ────────────────────────────────────────


def test_views_guide_has_a_heavy_views_section() -> None:
    text = _read(VIEWS_GUIDE)
    assert "## Heavy views (WebGL/WASM) {#heavy-views}" in text


def test_heavy_views_covers_the_five_rules() -> None:
    text = _read(VIEWS_GUIDE)
    heavy = text.split("## Heavy views", 1)[1].split("## Fork components", 1)[0]
    # (1) init only when visible and sized — the 0×0 hidden-frame trap.
    assert "ResizeObserver" in heavy
    assert "display: none" in heavy
    # (2) one engine per agent via the Web Locks API.
    assert "Web Locks API" in heavy
    assert "navigator.locks.request" in heavy
    # (3) no background:true for a heavy view.
    assert "Don't use `background: true` for a heavy view" in heavy
    # (5) dispose on pagehide, rebuild on pageshow.
    assert "pagehide" in heavy
    assert "pageshow" in heavy
    assert "WEBGL_lose_context" in heavy
    # Links to the multimodal recipe (r3).
    assert "/guides/plugins#let-the-model-see-it" in heavy


def test_heavy_views_quotes_the_real_publish_throttle_limits() -> None:
    text = _read(VIEWS_GUIDE)
    heavy = text.split("## Heavy views", 1)[1].split("## Fork components", 1)[0]
    assert "100 ms last-value-wins" in heavy
    assert "burst of 20 with" in heavy
    assert "10 tokens refilled per second" in heavy
    assert "256" in heavy


def test_publish_throttle_limits_are_read_off_the_code() -> None:
    # Pin the quoted numbers to createPublishThrottle so a code change forces a doc fix.
    src = _read(PLUGIN_VIEW_TSX)
    assert "100 ms last-value-wins windows, burst 20, refill 10/s" in src
    assert "Bound pending distinct topics to 256" in src
    assert "let tokens = 20;" in src
    assert "pending.size >= 256" in src


# ── r3: multimodal_tool_result recipe uses only the keys multimodal.py accepts ──


def test_plugins_guide_has_the_let_the_model_see_it_recipe() -> None:
    text = _read(PLUGINS_GUIDE)
    assert "### Let the model see it" in text
    assert "{#let-the-model-see-it}" in text
    assert "from graph.sdk import multimodal_tool_result" in text


def test_recipe_documents_only_the_accepted_image_keys() -> None:
    text = _read(PLUGINS_GUIDE)
    recipe = text.split("### Let the model see it", 1)[1].split("### Managed MCP servers", 1)[0]
    # The three keys multimodal_tool_result reads — and nothing invented.
    assert "`b64`" in recipe
    assert "`path`" in recipe
    assert "`mime`" in recipe
    assert "defaults to `image/png`" in recipe
    # No key the helper doesn't read should be documented as one.
    for bogus in ("`url`", "`data_uri`", "`src`", "`bytes`", "`filename`"):
        assert bogus not in recipe, f"recipe documents {bogus}, which multimodal.py does not accept"


def test_recipe_quotes_the_real_limits() -> None:
    text = _read(PLUGINS_GUIDE)
    recipe = text.split("### Let the model see it", 1)[1].split("### Managed MCP servers", 1)[0]
    assert "MAX_IMAGES_PER_RESULT" in recipe
    assert "MAX_IMAGE_BYTES" in recipe
    assert "2 MiB" in recipe  # MAX_IMAGE_BYTES = 2 * 1024 * 1024 decoded bytes per image


def test_recipe_links_the_skill_example_without_editing_it() -> None:
    text = _read(PLUGINS_GUIDE)
    assert "plugins/plugin-devkit/skills/building-plugins/SKILL.md" in text
    # The SKILL.md worked example must still be there to link to (we don't edit it).
    skill = _read(SKILL)
    assert "multimodal_tool_result" in skill
    assert "from graph.sdk import multimodal_tool_result" in skill


def test_multimodal_helper_accepts_exactly_b64_path_mime() -> None:
    # Source of truth for the key table: the helper reads b64/path/mime and nothing else.
    src = _read(MULTIMODAL)
    assert 'def multimodal_tool_result(text: str, images: list[dict]) -> str:' in src
    assert '{"b64": <base64 str>}' in src
    assert '{"path": <file path>}' in src
    assert '"mime"' in src
    assert "MAX_IMAGES_PER_RESULT = 3" in src
    assert "MAX_IMAGE_BYTES = 2 * 1024 * 1024" in src


def test_multimodal_tool_result_is_exported_from_graph_sdk() -> None:
    src = _read(SDK)
    assert "multimodal_tool_result" in src
