"""Drive protoagent-acp exactly as Zed does — spawn it on stdio, then
initialize → session/new → session/prompt — and print the session/update stream.

    python scripts/acp_harness.py --cwd ~/dev/protoAgent-team \
        --prompt "Read README.md's first 20 lines" \
        -- --url http://127.0.0.1:7875 --token-file ~/path/to/token

Everything after ``--`` is passed to the shim. Permission requests are DENIED by default
(the harness is for read-only probing of live agents); ``--approve`` selects the
``allow_once`` option instead, like a human clicking Allow in Zed, to drive an approval
flow such as ``run_command``; ``--approve-always`` picks ``allow_always`` when offered
("Allow for this session") and falls back to ``allow_once``. A failed turn arrives as a JSON-RPC error on
``session/prompt`` and is printed, not raised. ``--json`` prints each update as
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
    AllowedOutcome,
    RequestPermissionResponse,
)


class HarnessClient:
    def __init__(self, as_json: bool, approve: bool = False, approve_always: bool = False) -> None:
        self.as_json = as_json
        self.approve = approve
        self.approve_always = approve_always
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
        always = next((o for o in options if o.kind == "allow_always"), None)
        if self.approve_always and always is not None:
            pick, verdict = always, "ALLOW ALWAYS"
        elif self.approve or self.approve_always:
            pick = next((o for o in options if o.kind == "allow_once"), None) or next(o for o in options if o.kind.startswith("allow"))
            verdict = "ALLOW"
        else:
            pick = next((o for o in options if o.kind.startswith("reject")), options[-1])
            verdict = "DENY"
        print(f"{self._ts()} permission  {tool_call.title!r} → {verdict} ({pick.option_id})", flush=True)
        # The outcome field is Union[DeniedOutcome, AllowedOutcome] discriminated on `outcome`:
        # choosing ANY option (reject ones included) is an AllowedOutcome("selected");
        # DeniedOutcome("cancelled") is only for a prompt dismissed without a choice. The
        # bare SelectedPermissionOutcome base class is not serializable by the SDK sender.
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=pick.option_id))

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
    p.add_argument("--approve", action="store_true", help="answer permission requests with allow_once (default: deny)")
    p.add_argument("--approve-always", action="store_true", help="answer with allow_always when offered (else allow_once)")
    p.add_argument("--timeout", type=float, default=300.0)
    args = p.parse_args(argv)

    rc = 0
    client = HarnessClient(args.json, approve=args.approve, approve_always=args.approve_always)
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
            try:
                resp = await asyncio.wait_for(conn.prompt(session_id=sess.session_id, prompt=[text_block(text)]), args.timeout)
            except RequestError as exc:
                print(f"<<< error {exc.code}: {exc}  data={json.dumps(exc.data)[:400]}", flush=True)
                rc = 1
                continue
            print("<<< stopReason:", resp.stop_reason, "usage:", resp.usage.model_dump(by_alias=True, exclude_none=True) if resp.usage else None, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
