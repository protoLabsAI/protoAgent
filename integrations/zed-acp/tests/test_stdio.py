"""The shim as Zed runs it: a subprocess speaking ACP on stdio, driven by the SDK's
client side. Proves stdout carries only protocol (a stray print would break framing)."""

from __future__ import annotations

import sys
from typing import Any

import fake_a2a as fa
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block


class Client:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    def on_connect(self, conn: Any) -> None:
        pass


async def test_stdio_round_trip(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    with fa.FakeA2A(roots={"proj": "/abs/proj"}) as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.tool(ctx, "c1", "read_file", "started", args='{"project": "proj", "path": "README.md", "offset": 1}'),
            fa.tool(ctx, "c1", "read_file", "completed", result="# hi"),
            fa.text(ctx, "Done.", append=False),
            fa.done(ctx),
        ]
        client = Client()
        args = ["-m", "protoagent_acp", "--url", fake.url, "--token-file", str(token_file)]
        async with spawn_agent_process(client, sys.executable, *args) as (conn, _proc):
            init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
            assert init.protocol_version == PROTOCOL_VERSION
            assert {m.id for m in init.auth_methods} >= {"protoagent-login"}
            sess = await conn.new_session(cwd="/abs/proj", mcp_servers=[])
            resp = await conn.prompt(session_id=sess.session_id, prompt=[text_block("read the readme")])
    assert resp.stop_reason == "end_turn"
    kinds = [u["sessionUpdate"] for u in client.updates]
    assert kinds == ["tool_call", "tool_call_update", "agent_message_chunk"]
    assert client.updates[0]["locations"] == [{"path": "/abs/proj/README.md", "line": 1}]
