"""Tests for renderable chat components over A2A (ADR 0051 Slice 2) — the codec
(graph/components.py) and the show_component tool."""

from __future__ import annotations


from graph.components import (
    COMPONENT_MIME,
    COMPONENT_TYPES,
    encode_component,
    extract_component,
    strip_component,
)


class TestCodec:
    def test_roundtrip(self):
        s = "Rendered a table component. " + encode_component("table", {"columns": ["A", "B"], "rows": [["1", "2"]]})
        got = extract_component(s)
        assert got == {"component": "table", "props": {"columns": ["A", "B"], "rows": [["1", "2"]]}}
        assert strip_component(s) == "Rendered a table component."

    def test_unknown_component_type_rejected(self):
        s = encode_component("table", {})  # encode is dumb; tamper the type
        s = s.replace('"table"', '"nope"')
        assert extract_component(s) is None

    def test_no_sentinel_returns_none(self):
        assert extract_component("just a normal tool result") is None
        assert strip_component("just a normal tool result") == "just a normal tool result"

    def test_malformed_json_returns_none(self):
        from graph.components import _SENTINEL

        assert extract_component(_SENTINEL + "{not json") is None

    def test_props_defaults_to_empty_dict(self):
        s = encode_component("keyvalue", {"items": []})
        s = s.replace('{"items": []}', "null")  # props=null on the wire
        got = extract_component(s)
        assert got is not None and got["props"] == {}

    def test_mime_and_types(self):
        assert COMPONENT_MIME.endswith("component-v1+json")
        assert set(COMPONENT_TYPES) == {"table", "keyvalue", "timeline"}

    def test_large_payload_extracts_but_truncated_preview_does_not(self):
        """Regression (#1323): a rich component (a 9-step timeline) exceeds the tool-card
        preview cap. extract_component MUST run on the FULL tool content — extracting from the
        truncated card preview (what server/chat.py used to do) cuts the JSON tail and fails."""
        from server.chat import _TOOL_PREVIEW_CHARS

        steps = [
            {"label": f"Phase {i}: build the thing", "state": "todo", "detail": "x" * 80} for i in range(9)
        ]
        full = "Rendered a timeline component for the user. " + encode_component("timeline", {"steps": steps})
        assert len(full) > _TOOL_PREVIEW_CHARS  # the payload is bigger than the card preview
        # Full content extracts the complete payload …
        got = extract_component(full)
        assert got is not None and len(got["props"]["steps"]) == 9
        # … but the truncated preview (the old bug) loses the JSON tail and fails.
        assert extract_component(full[:_TOOL_PREVIEW_CHARS]) is None


class TestShowComponentTool:
    def _tool(self):
        from tools.lg_tools import get_all_tools

        tools = {t.name: t for t in get_all_tools()}
        return tools["show_component"]

    async def test_valid_emits_sentinel_payload(self):
        out = await self._tool().ainvoke(
            {"component": "keyvalue", "props": {"items": [{"label": "Credits", "value": "183k"}]}, "title": "Wallet"}
        )
        comp = extract_component(out)
        assert comp is not None
        assert comp["component"] == "keyvalue"
        assert comp["props"]["title"] == "Wallet"  # title folded into props
        assert comp["props"]["items"] == [{"label": "Credits", "value": "183k"}]

    async def test_unknown_component_errors_without_sentinel(self):
        out = await self._tool().ainvoke({"component": "barchart", "props": {}})
        assert out.startswith("Error:")
        assert extract_component(out) is None
