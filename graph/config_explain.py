"""``config explain`` — the read-only diagnostic that answers "where did my
config / key go?".

It prints this instance's **identity** (the env-derived id + the three roots),
**every resolved on-disk path** (so you can see exactly which file a store lives
in), and the **per-field cascade provenance** — for each settings key, the scope
it homes at (agent leaf vs box-shared Host layer), the live value, and which
cascade layer that value actually came from (agent / host / default).

This is the non-destructive replacement for the old self-heal: it never moves or
rewrites anything, it just shows you the resolution. The same builder powers both
the CLI (``python -m server config explain``) and the operator API
(``GET /api/config/explain``), so they can't drift.

Lives in ``graph/`` (a leaf layer): it imports only ``infra.paths`` + ``graph.*``,
never ``server``/``operator_api`` — so the CLI front-end *and* the operator route
can both import it without crossing the import-layering contract.
"""

from __future__ import annotations

from typing import Any


def build_config_explain(config: Any = None) -> dict:
    """Assemble the full explain payload: id + roots + every resolved path
    (from :meth:`InstancePaths.explain`) plus the per-field ``cascade`` list.

    ``config`` lets the operator route pass the LIVE ``STATE.graph_config`` (what's
    actually running); when ``None`` (the CLI path) the agent leaf is loaded fresh
    from disk via the standard cascade, so the diagnostic reflects the on-disk
    config even with no server running.
    """
    from infra.paths import instance_paths

    out = instance_paths().explain()  # {instance_id, box_root, instance_root, app_root, paths}
    out["cascade"] = _build_cascade(config)
    return out


def _build_cascade(config: Any = None) -> list[dict]:
    """Per-field provenance, reusing the settings-schema source logic (one source
    of truth with the Settings UI). Loads the live config, the raw agent leaf doc,
    and the filtered Host layer, then asks ``build_schema`` to stamp each field's
    ``source`` (agent / host / default). Returns ``[{key, scope, value, source}]``.

    Secrets are never echoed: ``build_schema`` already redacts secret-typed fields
    to ``value: ""`` + ``is_set``; we surface that as a ``"<set>"`` / ``"<unset>"``
    marker so the operator can see a key IS configured without printing it.
    """
    from graph.config import LangGraphConfig, _load_host_layer
    from graph.config_io import config_yaml_path, load_yaml_doc
    from graph.settings_schema import build_schema

    cfg_yaml = config_yaml_path()
    if config is None:
        config = LangGraphConfig.from_yaml(cfg_yaml)
    # Per-layer provenance (ADR 0047), exactly as /api/settings/schema reads it: the
    # raw agent leaf doc + the host-key-filtered Host layer drive build_schema's
    # `source` stamp. No gateway probe (model_options=None) — explain stays offline.
    agent_doc = load_yaml_doc(cfg_yaml) if cfg_yaml.exists() else {}
    if not isinstance(agent_doc, dict):
        agent_doc = {}
    host_doc = _load_host_layer()

    cascade: list[dict] = []
    for group in build_schema(config, agent_doc=agent_doc, host_doc=host_doc):
        for f in group["fields"]:
            if f.get("type") == "secret":
                value: Any = "<set>" if f.get("is_set") else "<unset>"
            else:
                value = f.get("value")
            cascade.append(
                {
                    "key": f["key"],
                    "scope": f.get("scope", "agent"),
                    "value": value,
                    "source": f.get("source", "default"),
                }
            )
    return cascade


# ---------------------------------------------------------------------------
# CLI front-end (`python -m server config explain`)
# ---------------------------------------------------------------------------


def render_config_explain(data: dict) -> str:
    """Human-readable rendering of a :func:`build_config_explain` payload: an
    identity block, a path list, and a compact cascade table."""
    lines: list[str] = []
    lines.append("Instance")
    lines.append(f"  id:             {data['instance_id']}")
    lines.append(f"  box root:       {data['box_root']}")
    lines.append(f"  instance root:  {data['instance_root']}")
    lines.append(f"  app root:       {data['app_root']}")

    paths = data.get("paths", {})
    lines.append("")
    lines.append("Paths")
    width = max((len(k) for k in paths), default=0)
    for key in sorted(paths):
        lines.append(f"  {key.ljust(width)}  {paths[key]}")

    cascade = data.get("cascade", [])
    lines.append("")
    lines.append("Cascade (per-field provenance — source = which layer set the live value)")
    if cascade:
        kw = max(len(c["key"]) for c in cascade)
        sw = max(len(str(c["scope"])) for c in cascade)
        ow = max(len(str(c["source"])) for c in cascade)
        header = f"  {'KEY'.ljust(kw)}  {'SCOPE'.ljust(sw)}  {'SOURCE'.ljust(ow)}  VALUE"
        lines.append(header)
        for c in cascade:
            val = "" if c["value"] is None else str(c["value"])
            lines.append(f"  {c['key'].ljust(kw)}  {str(c['scope']).ljust(sw)}  {str(c['source']).ljust(ow)}  {val}")
    else:
        lines.append("  (no settings fields)")
    return "\n".join(lines)


def _parse_set_pairs(pairs: list[str]) -> dict:
    """``["a.b=1", "x=true"]`` → a flat dotted map with JSON-typed values (so ``8080``/
    ``true`` become an int/bool; a bare word stays a string). ``nest_updates`` then nests it."""
    import json

    flat: dict = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"expected key=value, got {p!r}")
        key, val = p.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"empty key in {p!r}")
        try:
            flat[key] = json.loads(val)
        except ValueError:
            flat[key] = val  # a bare string
    return flat


def run_config_cli(argv: list[str]) -> int:
    """``protoagent config <action>`` dispatch: ``explain`` (read-only diagnostic),
    ``get`` (print config.yaml), ``set key=value …`` (write config.yaml on disk, via the
    shared ``ops.config.set`` op). ``--json`` on explain/get emits the raw payload."""
    import argparse
    import asyncio
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="protoagent config",
        description="Inspect and edit this instance's config.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    ep = sub.add_parser(
        "explain",
        help="print this instance's identity, roots, every resolved path, and per-field cascade provenance",
    )
    ep.add_argument("--json", action="store_true", help="emit the raw payload as JSON instead of the human table")
    gp = sub.add_parser("get", help="print the on-disk config.yaml")
    gp.add_argument("--json", action="store_true", help="emit JSON instead of YAML")
    sp = sub.add_parser("set", help="write config keys to config.yaml on disk (dotted key=value, JSON-typed)")
    sp.add_argument("pairs", nargs="+", metavar="key=value", help="e.g. fleet.mdns.enabled=false server.port=7871")
    args = parser.parse_args(argv)

    if args.action == "explain":
        data = build_config_explain()
        print(json.dumps(data, indent=2) if args.json else render_config_explain(data))
        return 0

    if args.action == "get":
        from ops.config import get_config

        cfg = asyncio.run(get_config(ctx=None))  # ctx=None ⇒ read the on-disk doc
        if args.json:
            print(json.dumps(cfg, indent=2))
        else:
            import yaml

            print(yaml.safe_dump(cfg, sort_keys=False).rstrip())
        return 0

    if args.action == "set":
        from graph.settings_schema import nest_updates
        from ops.config import set_config

        try:
            flat = _parse_set_pairs(args.pairs)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        result = asyncio.run(set_config(nest_updates(flat), apply_settings=None))  # disk-only
        for m in result.messages:
            print(m)
        if not result.reloaded:
            print("(written to disk — restart / reload a running server to apply)", file=sys.stderr)
        return 0 if result.ok else 1

    return 2  # unreachable: argparse rejects an unknown action first
