#!/usr/bin/env python3
"""Generate an NVIDIA OpenShell sandbox policy from protoAgent config (ADR 0008).

Reads ``langgraph-config.yaml`` and emits a least-privilege starter policy in
OpenShell's **v1 policy schema** (validated against OpenShell v0.0.59) —
derived from what the agent is actually configured to use, not hand-rolled
guesses:

- ``filesystem_policy`` (Landlock) ← the ``filesystem.projects`` registry
  (read-only vs read-write per project) + the agent data root + a read-only
  OS baseline.
- ``network_policies`` (per-binary, deny-by-default egress proxy) ←
  ``egress.allowed_hosts`` + ``model.api_base``.
- ``process`` ← runs as the unprivileged ``sandbox`` user from the image.
- ``landlock.compatibility: best_effort`` ← missing baseline paths are
  skipped instead of aborting startup.

Schema reference: https://github.com/NVIDIA/OpenShell — docs/reference/policy-schema.
Top-level fields are ``version`` / ``filesystem_policy`` / ``landlock`` /
``process`` / ``network_policies``. There is no ``inference`` domain in v1;
model-credential injection is handled by OpenShell *providers* instead.

Usage:
    python scripts/gen_openshell_policy.py [--config config/langgraph-config.yaml] [--out policy.yaml]

NOTE: OpenShell is pre-1.0 — re-verify against your installed release when
upgrading. ``openshell policy prove`` can check properties of the output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from graph.config import LangGraphConfig  # noqa: E402

# Read-only OS baseline for the python:3.12-slim image. Missing paths are
# skipped under landlock best_effort, so this is safe across image variants.
_RO_BASELINE = ["/usr", "/lib", "/lib64", "/etc", "/proc", "/opt/protoagent", "/dev/urandom"]
# Writable surfaces matching the Dockerfile (USER sandbox, /sandbox data root).
# /opt/protoagent/config is the live-config dir ensure_live_config() copies
# into at boot (a named volume under docker-compose; just a writable dir here).
_RW_BASELINE = ["/sandbox", "/tmp", "/dev/null", "/home/sandbox", "/opt/protoagent/config"]
# Executables allowed to use the egress allowlist: the agent process itself
# plus the CLI tools the image ships for model-driven shell use.
_EGRESS_BINARIES = ["/usr/local/bin/python*", "/usr/bin/git", "/usr/bin/curl", "/usr/bin/gh"]


def _host_port(url_or_host: str) -> tuple[str, int]:
    """Split a config value into (host, port). Bare hosts default to 443."""
    raw = str(url_or_host or "").strip()
    if not raw:
        return "", 0
    u = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (u.hostname or "").strip()
    port = u.port or (80 if u.scheme == "http" else 443)
    return host, port


def build_policy(cfg: LangGraphConfig) -> str:
    rw_paths: list[tuple[str, str]] = [(p, "") for p in _RW_BASELINE]
    ro_paths: list[tuple[str, str]] = [(p, "") for p in _RO_BASELINE]
    for entry in cfg.filesystem_projects or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        target = rw_paths if entry.get("write") else ro_paths
        target.append((path, f"project: {entry.get('name', '?')}"))

    # Egress allowlist: configured hosts + the inference gateway. Deny everything else.
    endpoints: list[tuple[str, int, str]] = []
    api_host, api_port = _host_port(getattr(cfg, "api_base", ""))
    if api_host:
        endpoints.append((api_host, api_port, "model / inference gateway"))
    for h in cfg.egress_allowed_hosts or []:
        host, port = _host_port(h)
        if host and (host, port) != (api_host, api_port):
            endpoints.append((host, port, ""))

    def _paths(items: list[tuple[str, str]]) -> str:
        return "\n".join(
            f"    - {p}" + (f"  # {note}" if note else "") for p, note in items
        )

    def _endpoints() -> str:
        if not endpoints:
            return (
                "      []  # none configured — set egress.allowed_hosts and/or"
                " model.api_base; default-deny blocks all egress"
            )
        out = []
        for host, port, note in endpoints:
            out.append(f"      - host: {host}" + (f"  # {note}" if note else ""))
            out.append(f"        port: {port}")
        return "\n".join(out)

    return f"""\
# OpenShell sandbox policy for protoAgent — GENERATED (ADR 0008). Do not hand-edit;
# regenerate:  python scripts/gen_openshell_policy.py --config <config.yaml>
#
# v1 policy schema, validated against OpenShell v0.0.59. Re-verify on upgrade
# (`openshell policy prove` can check properties of this file). Single source
# of truth: filesystem_policy ← filesystem.projects; network_policies ←
# egress.allowed_hosts + model.api_base.

version: 1

filesystem_policy:
  # Landlock allowed paths, locked at sandbox creation. Anything not listed is
  # inaccessible — the kernel-enforced version of protoAgent's in-process fence.
  include_workdir: true
  read_write:
{_paths(rw_paths)}
  read_only:
{_paths(ro_paths)}

landlock:
  # best_effort skips baseline paths missing from the image instead of
  # aborting; switch to hard_requirement where any isolation gap must abort.
  compatibility: best_effort

process:
  # The unprivileged user baked into the protoAgent image (UID 1001).
  run_as_user: sandbox
  run_as_group: sandbox

network_policies:
  # Deny-by-default egress. Only the binaries below may reach the endpoints
  # below; everything else is blocked by the per-sandbox proxy (the in-process
  # egress allowlist still applies inside as defense-in-depth).
  agent_egress:
    name: protoagent-egress
    endpoints:
{_endpoints()}
    binaries:
{chr(10).join(f"      - path: {b}" for b in _EGRESS_BINARIES)}
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
