#!/usr/bin/env python3
"""Generate an NVIDIA OpenShell sandbox policy from protoAgent config (ADR 0008).

Reads ``langgraph-config.yaml`` and emits a least-privilege starter policy in
OpenShell's documented four-domain model — derived from what the agent is
actually configured to use, not hand-rolled guesses:

- **filesystem** (Landlock) ← the ``filesystem.projects`` registry (read-only vs
  read-write per project) + the agent data root.
- **network** (OPA proxy, deny-by-default) ← ``egress.allowed_hosts`` +
  ``model.api_base`` + common fleet endpoints.
- **process** ← default seccomp.
- **inference** ← pinned to the model gateway.

Usage:
    python scripts/gen_openshell_policy.py [--config config/langgraph-config.yaml] [--out policy.yaml]

NOTE: targets OpenShell's *documented* schema — verify field names against your
installed OpenShell release before applying. It's a generated starting point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from graph.config import LangGraphConfig  # noqa: E402


def _host(url: str) -> str:
    if not url:
        return ""
    u = url if "://" in url else f"//{url}"
    return (urlparse(u).hostname or "").strip()


def build_policy(cfg: LangGraphConfig) -> str:
    rw_paths: list[str] = ["/sandbox  # agent data root (stores, checkpoints, telemetry, memory)"]
    ro_paths: list[str] = []
    for entry in cfg.filesystem_projects or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        (rw_paths if entry.get("write") else ro_paths).append(f"{path}  # project: {entry.get('name', '?')}")

    # Egress allowlist: configured hosts + the inference gateway. Deny everything else.
    hosts: list[str] = []
    api_host = _host(getattr(cfg, "api_base", ""))
    if api_host:
        hosts.append(f"{api_host}  # model / inference gateway")
    for h in cfg.egress_allowed_hosts or []:
        hosts.append(str(h))
    if not hosts:
        hosts.append("# (none configured — set egress.allowed_hosts; default-deny blocks all egress)")

    def _block(items: list[str], indent: str = "    ") -> str:
        return "\n".join(f"{indent}- {i}" for i in items) if items else f"{indent}[]"

    return f"""\
# OpenShell sandbox policy for protoAgent — GENERATED (ADR 0008). Do not hand-edit;
# regenerate:  python scripts/gen_openshell_policy.py --config <config.yaml>
#
# Targets OpenShell's documented four-domain model (filesystem/network/process/
# inference). VERIFY field names against your installed OpenShell release.
# Single source of truth: filesystem ← filesystem.projects; network ←
# egress.allowed_hosts + model.api_base.

filesystem:
  # Landlock allowed paths, locked at sandbox creation. Nothing else is readable
  # or writable — the kernel-enforced version of protoAgent's in-process fence.
  read_write:
{_block(rw_paths)}
  read_only:
{_block(ro_paths) if ro_paths else "    []  # (no read-only projects registered)"}

network:
  # Deny-by-default egress via the OPA proxy. Only these hosts are reachable.
  default: deny
  allow:
{_block(hosts)}

process:
  # Default seccomp — blocks ptrace / mount / pivot_root / clone+unshare / raw
  # sockets. Gives execute_code/run_command real syscall filtering.
  seccomp: default

inference:
  # Pin model calls to the gateway; strip caller credentials on the way out.
  route_to: {getattr(cfg, 'api_base', '') or '# set model.api_base'}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate an OpenShell policy from protoAgent config (ADR 0008).")
    ap.add_argument("--config", default=str(_REPO / "config" / "langgraph-config.yaml"))
    ap.add_argument("--out", default="", help="write to this file (default: stdout)")
    args = ap.parse_args()

    cfg = LangGraphConfig.from_yaml(args.config)
    policy = build_policy(cfg)
    if args.out:
        Path(args.out).write_text(policy, encoding="utf-8")
        print(f"wrote OpenShell policy → {args.out}", file=sys.stderr)
    else:
        print(policy)


if __name__ == "__main__":
    main()
