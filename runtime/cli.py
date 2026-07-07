"""``protoagent runtime`` — select the agent runtime (ADR 0033) from the terminal,
plus the one-command **Hermes preset** (``protoagent hermes``).

The preset targets an existing Hermes user: they keep their agent — identity, memory,
skills, model endpoint, all living in ``~/.hermes`` — and protoAgent wraps around it as
the shell (console, A2A, scheduler, goals). Concretely, ``protoagent hermes``:

1. installs ``hermes-acp`` if missing — ``uv tool install 'hermes-agent[acp]' --with
   mcp==1.26.0``. The ``--with`` pin matters: the ``[acp]`` extra does NOT pull the
   ``mcp`` SDK, and without it Hermes silently skips MCP registration, so protoAgent's
   operator tools would never appear in its toolset;
2. makes the two model configs agree, **Hermes wins**: an endpoint found in
   ``~/.hermes/config.yaml`` is imported into this instance (aux calls — compaction,
   goal-eval — need a model too); only when Hermes has none and this instance does is
   the seeding reversed. Neither side's explicit config is ever clobbered;
3. adopts ``~/.hermes/SOUL.md`` as this instance's persona — only while ours is still
   the shipped placeholder (and via ``write_soul``, so it lands in soul history);
4. sets ``agent_runtime: acp:hermes``.

Every bootstrap step is best-effort — a failed install or unreadable Hermes config
prints a warning and the runtime flip still lands.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The install line, exactly (see module docstring for why --with mcp is load-bearing).
_HERMES_INSTALL = ["uv", "tool", "install", "hermes-agent[acp]", "--with", "mcp==1.26.0"]

# Non-empty placeholder for keyless local endpoints — same contract as
# ``graph.model_cli._LOCAL_KEY_PLACEHOLDER`` (the OpenAI client refuses an empty key).
_KEY_PLACEHOLDER = "local"


def _hermes_home() -> Path:
    """Hermes's state dir — it honors ``HERMES_HOME``, so we do too."""
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


# ── reading each side's model config ─────────────────────────────────────────


def _read_hermes_model(home: Path) -> dict | None:
    """The model endpoint Hermes is configured for, or None.

    Reads ``config.yaml``'s ``model`` block (``default``/``base_url``/``api_key``),
    falling back to the first usable ``custom_providers`` entry. Only an
    OpenAI-compatible endpoint (a ``base_url``) is importable — an OAuth provider
    (Nous Portal etc.) has no key we could lift, so it returns None and this
    instance's model config is left for the operator.
    """
    import yaml

    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return None
    try:
        doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — unreadable Hermes config ⇒ nothing to import
        return None

    candidates: list[dict] = []
    model = doc.get("model")
    if isinstance(model, dict):
        candidates.append(
            {"model": model.get("default"), "base_url": model.get("base_url"), "api_key": model.get("api_key")}
        )
    for entry in doc.get("custom_providers") or []:
        if isinstance(entry, dict):
            candidates.append(
                {"model": entry.get("model"), "base_url": entry.get("base_url"), "api_key": entry.get("api_key")}
            )
    for c in candidates:
        if c.get("model") and c.get("base_url"):
            return {"model": str(c["model"]), "base_url": str(c["base_url"]), "api_key": str(c.get("api_key") or "")}
    return None


def _instance_model_configured() -> bool:
    """Has THIS instance an explicitly configured model? (doc ``model`` block with any
    of api_base/name/api_key set, or an api_key in the sibling secrets.yaml). The
    dataclass defaults don't count — those are what the preset exists to fill in."""
    import yaml

    from graph.config_io import load_yaml_doc, secrets_yaml_path

    doc = load_yaml_doc()
    model = doc.get("model") if isinstance(doc, dict) or hasattr(doc, "get") else None
    if isinstance(model, dict) and any(str(model.get(k) or "").strip() for k in ("api_base", "name", "api_key")):
        return True
    sp = secrets_yaml_path()
    if sp.exists():
        try:
            secrets = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            if str((secrets.get("model") or {}).get("api_key") or "").strip():
                return True
        except Exception:  # noqa: BLE001 — unreadable secrets ⇒ treat as unconfigured
            pass
    return False


# ── the two seeding directions (never clobber an explicit config) ────────────


def _import_hermes_model_into_instance(hm: dict) -> None:
    """Hermes → protoAgent: write Hermes's endpoint as this instance's model config
    (same doc keys ``protoagent model use`` writes)."""
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc()
    model = doc.get("model")
    if not isinstance(model, dict):
        model = {}
        doc["model"] = model
    model["provider"] = "openai"
    model["api_base"] = hm["base_url"]
    model["name"] = hm["model"]
    model["api_key"] = hm["api_key"] or _KEY_PLACEHOLDER
    save_yaml_doc(doc)
    print(f"model: imported from Hermes — {hm['model']} @ {hm['base_url']}")


def _seed_hermes_from_instance(home: Path) -> None:
    """protoAgent → Hermes: give a fresh Hermes this instance's endpoint, as a
    ``custom`` provider. Merges into an existing config.yaml; only fills a missing
    ``model.default`` (an explicit Hermes model choice is respected)."""
    import yaml

    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    if not (cfg.api_base or "").strip():
        return

    cfg_path = home / "config.yaml"
    doc: dict = {}
    if cfg_path.exists():
        try:
            doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — corrupt file: don't guess, don't overwrite
            print(f"hermes: could not parse {cfg_path} — leaving it alone", file=sys.stderr)
            return
    model = doc.get("model")
    if not isinstance(model, dict):
        model = {}
        doc["model"] = model
    if str(model.get("default") or "").strip():
        return  # Hermes already points at a model — respect it
    model["default"] = cfg.model_name
    model["provider"] = "custom"
    model["base_url"] = cfg.api_base
    model["api_key"] = cfg.api_key or _KEY_PLACEHOLDER
    providers = doc.setdefault("custom_providers", [])
    if not any(isinstance(p, dict) and p.get("base_url") == cfg.api_base for p in providers):
        providers.append(
            {
                "name": "protoagent-gateway",
                "base_url": cfg.api_base,
                "api_key": cfg.api_key or _KEY_PLACEHOLDER,
                "model": cfg.model_name,
            }
        )
    home.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    print(f"hermes: seeded {cfg_path} with {cfg.model_name} @ {cfg.api_base}")


def _adopt_hermes_soul(home: Path) -> None:
    """Adopt ``~/.hermes/SOUL.md`` as this instance's persona — a Hermes user should
    hear THEIR agent in the console, not the shipped placeholder. Never overwrites a
    persona the operator has already customized; ``write_soul`` archives to history."""
    from graph.config_io import _is_placeholder_soul, read_soul, write_soul

    theirs_path = home / "SOUL.md"
    if not theirs_path.exists():
        return
    try:
        theirs = theirs_path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 — unreadable ⇒ nothing to adopt
        return
    ours = read_soul()
    if theirs and (not ours.strip() or _is_placeholder_soul(ours)):
        write_soul(theirs)
        print(f"soul: adopted {theirs_path} as this instance's persona")


# ── install ──────────────────────────────────────────────────────────────────


def _acp_venv_python(acp_path: str) -> str | None:
    """The venv python behind the ``hermes-acp`` entry script, from its shebang —
    where the ``mcp`` SDK must be importable for MCP mounting to work."""
    try:
        first = Path(acp_path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except Exception:  # noqa: BLE001 — binary/unreadable launcher: can't introspect
        return None
    return first[2:].strip() if first.startswith("#!") and "python" in first else None


def _ensure_hermes_installed() -> None:
    """Install (or repair) hermes-acp — best-effort, one printed line per outcome."""
    have_uv = shutil.which("uv") is not None
    acp = shutil.which("hermes-acp")
    if acp is None:
        if not have_uv:
            print(
                "hermes: hermes-acp not found and uv is not installed — install it yourself:\n"
                f"  {' '.join(_HERMES_INSTALL)}",
                file=sys.stderr,
            )
            return
        print(f"hermes: installing — {' '.join(_HERMES_INSTALL)}")
        if subprocess.run(_HERMES_INSTALL, check=False).returncode != 0:
            print("hermes: install failed — run it manually, then re-run this command", file=sys.stderr)
        return
    # Pre-existing install: verify the mcp SDK is importable in ITS venv (the silent
    # failure mode this whole pin exists for).
    py = _acp_venv_python(acp)
    if py and subprocess.run([py, "-c", "import mcp"], capture_output=True, check=False).returncode != 0:
        if have_uv:
            print("hermes: mcp SDK missing from the hermes-acp venv — reinstalling with the pin")
            subprocess.run([*_HERMES_INSTALL[:3], "--force", *_HERMES_INSTALL[3:]], check=False)
        else:
            print(
                f"hermes: mcp SDK missing from the hermes-acp venv — protoAgent's tools won't mount.\n"
                f"  Fix: {' '.join(_HERMES_INSTALL)}  (with --force)",
                file=sys.stderr,
            )


def _bootstrap_hermes() -> None:
    """The preset, minus the runtime flip (which the caller always does)."""
    home = _hermes_home()
    _ensure_hermes_installed()
    try:
        hm = _read_hermes_model(home)
        if hm and not _instance_model_configured():
            _import_hermes_model_into_instance(hm)
        elif not hm and _instance_model_configured():
            _seed_hermes_from_instance(home)
        elif not hm:
            print(
                "hermes: no model configured on either side — run `hermes model` (or "
                "`protoagent model use ...`), then re-run this command",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — seeding must never block the runtime flip
        print(f"hermes: model seeding skipped ({exc})", file=sys.stderr)
    try:
        _adopt_hermes_soul(home)
    except Exception as exc:  # noqa: BLE001
        print(f"hermes: soul adoption skipped ({exc})", file=sys.stderr)


# ── subcommands ──────────────────────────────────────────────────────────────


def _known_runtimes(config=None) -> list[str]:
    from runtime.acp_agents import acp_runtime_options

    known = ["native", *acp_runtime_options()]
    for agent in (getattr(config, "acp_agents", None) or {}) if config else {}:
        if f"acp:{agent}" not in known:
            known.append(f"acp:{agent}")
    return known


def _cmd_use(args) -> int:
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path, load_yaml_doc, save_yaml_doc

    target = (args.runtime or "").strip()
    if target == "hermes":  # the preset spelling — `protoagent runtime use hermes`
        target = "acp:hermes"
    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    known = _known_runtimes(cfg)
    if target not in known:
        print(f"runtime use: unknown runtime {target!r} — one of: {', '.join(known)}", file=sys.stderr)
        return 2

    if target == "acp:hermes" and not args.no_bootstrap:
        _bootstrap_hermes()

    doc = load_yaml_doc()
    doc["agent_runtime"] = target
    save_yaml_doc(doc)
    print(f"runtime: now {target}")
    print("Start it with:  protoagent up   (console: http://127.0.0.1:7870)")
    return 0


def _cmd_list(_args) -> int:
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from runtime.acp_agents import acp_agent_catalog

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    print(f"current:  {cfg.agent_runtime}")
    print("options:")
    print("  native        (built-in LangGraph loop)")
    for a in acp_agent_catalog():
        status = "installed" if shutil.which(a["command"]) else f"command {a['command']!r} not found"
        print(f"  acp:{a['id']:<10}{a['label']}  [{status}]")
    return 0


def run_runtime_cli(argv: list[str]) -> int:
    """`protoagent runtime` — see module docstring. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="protoagent runtime",
        description="Select the agent runtime — native (LangGraph) or an ACP agent (ADR 0033).",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<use|list>")

    p_use = sub.add_parser("use", help="Switch runtime (writes the live config); `use hermes` runs the full preset")
    p_use.add_argument("runtime", help="native | hermes | acp:<agent>  (see `runtime list`)")
    p_use.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="hermes only: skip install/config seeding — just flip agent_runtime",
    )
    p_use.set_defaults(fn=_cmd_use)

    sub.add_parser("list", help="Show the current runtime + available ones (with install status)").set_defaults(
        fn=_cmd_list
    )

    args = parser.parse_args(argv)
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return 0
    return fn(args)


def run_hermes_cli(argv: list[str]) -> int:
    """`protoagent hermes` — sugar for `protoagent runtime use hermes` (flags pass through)."""
    return run_runtime_cli(["use", "hermes", *argv])
