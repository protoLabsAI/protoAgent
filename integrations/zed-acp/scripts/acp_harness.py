"""Drive protoagent-acp exactly as Zed does — spawn it on stdio, then
initialize → session/new → session/prompt — and print the session/update stream.

    python scripts/acp_harness.py --cwd ~/dev/protoAgent-team \
        --prompt "Read README.md's first 20 lines" \
        -- --url http://127.0.0.1:7875 --token-file ~/path/to/token

Everything after ``--`` is passed to the shim. Permission requests are always DENIED
(this harness is for read-only probing of live agents). ``--json`` prints each update as
the raw ACP JSON instead of the one-line summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process, text_block
from acp.schema import (
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
    SelectedPermissionOutcome,
)


class HarnessClient:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.t0 = time.monotonic()
        self.updates: list[dict] = []

    def _ts(self) -> str:
        return f"{time.monotonic() - self.t0:6.1f}s"

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        d = update.model_dump(by_alias=True, exclude_none=True)
        self.updates.append(d)
        if self.as_json:
            print(json.dumps(d, ensure_ascii=False), flush=True)
            return
        kind = d.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            text = (d.get("content") or {}).get("text", "")
            tag = "text " if kind == "agent_message_chunk" else "think"
            print(f"{self._ts()} {tag} {text!r}", flush=True)
        elif kind in ("tool_call", "tool_call_update"):
            bits = [d.get("toolCallId", "")[:14]]
            for k in ("kind", "status", "title"):
                if d.get(k):
                    bits.append(f"{k}={d[k]!r}")
            if d.get("locations"):
                bits.append("locations=" + json.dumps(d["locations"]))
            print(f"{self._ts()} {kind:16} " + " ".join(bits), flush=True)
        else:
            print(f"{self._ts()} {kind} {json.dumps(d)[:200]}", flush=True)

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **_: Any) -> RequestPermissionResponse:
        deny = next((o for o in options if o.kind.startswith("reject")), options[-1])
        print(f"{self._ts()} permission  {tool_call.title!r} → DENY ({deny.option_id})", flush=True)
        return RequestPermissionResponse(outcome=SelectedPermissionOutcome(outcome="selected", option_id=deny.option_id))

    async def read_text_file(self, *a: Any, **k: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, *a: Any, **k: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    def on_connect(self, conn: Any) -> None:
        pass


async def main() -> int:
    argv = sys.argv[1:]
    shim_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, shim_args = argv[:i], argv[i + 1 :]
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", action="append", required=True, help="repeat for a multi-turn session")
    p.add_argument("--cwd", default=os.getcwd(), help="the editor folder sent in session/new")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=300.0)
    args = p.parse_args(argv)

    client = HarnessClient(args.json)
    async with spawn_agent_process(client, sys.executable, "-m", "protoagent_acp", *shim_args) as (conn, _proc):
        init = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(fs=FileSystemCapabilities(read_text_file=False, write_text_file=False), terminal=False),
            client_info=Implementation(name="acp-harness", title="ACP harness", version="0"),
        )
        print("initialize →", json.dumps(init.model_dump(by_alias=True, exclude_none=True)), flush=True)
        sess = await conn.new_session(cwd=os.path.expanduser(args.cwd), mcp_servers=[])
        print("session/new →", sess.session_id, flush=True)
        for text in args.prompt:
            print(f"\n>>> session/prompt {text!r}", flush=True)
            resp = await asyncio.wait_for(conn.prompt(session_id=sess.session_id, prompt=[text_block(text)]), args.timeout)
            print("<<< stopReason:", resp.stop_reason, "usage:", resp.usage.model_dump(by_alias=True, exclude_none=True) if resp.usage else None, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
