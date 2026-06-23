"""Tests for renderable chat components over A2A (ADR 0051 Slice 2) — the codec
(graph/components.py) and the show_component tool."""

from __future__ import annotations

import pytest

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


@pytest.mark.skip(reason="show_component temporarily disabled — see issue #1323 (the codec above stays tested)")
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
