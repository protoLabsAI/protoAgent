"""``protoagent model`` — point protoAgent at a local (or any OpenAI-compatible) LLM.

ADR 0075 D5. protoAgent's model is just gateway config — ``graph.llm.create_llm``
builds a plain ``ChatOpenAI(base_url, model)``, so the LiteLLM gateway is the
*default, not a lock-in*. This command writes that config non-interactively, so
pointing at a local Ollama / LM Studio / llama.cpp / vLLM endpoint is one line:

    protoagent model use --base-url http://127.0.0.1:8080/v1 --model qwen2.5

That one-liner is also the copy-paste target for HuggingFace's "Use this model"
local-app snippet (the hermes-agent / openclaw / pi agent-runtime pattern): a HF
model card hands the model id straight to protoAgent.
"""

from __future__ import annotations

import argparse
import sys

# Well-known local OpenAI-compatible endpoints, probed by ``discover`` / ``list``.
_LOCAL_ENDPOINTS: list[tuple[str, str]] = [
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("lm-studio", "http://127.0.0.1:1234/v1"),
    ("llama.cpp / vLLM", "http://127.0.0.1:8080/v1"),
]

# A non-empty placeholder key. A LOOPBACK endpoint ignores it, but the OpenAI client
# constructor requires *some* key (empty → "Missing credentials"). Not a secret, so it's
# fine inline. It is only ever written for a loopback base URL: writing it for a remote
# gateway invented a credential that endpoint never accepts, and because the value is
# non-blank, `protoagent setup` then read it as "a key is configured" and declared setup
# complete — the agent only failed at the first turn, with a 401 (#2579).
_LOCAL_KEY_PLACEHOLDER = "local"

# Hosts that can only be this machine, where the placeholder is honest.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"})


def _is_loopback_endpoint(base_url: str) -> bool:
    """True when ``base_url`` points at this machine — the case a placeholder key fits.
    Anything else (a LAN host, a remote gateway) needs a real credential."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(base_url).hostname or "").strip().lower()
    except ValueError:  # malformed URL ⇒ don't claim it's local
        return False
    return host in _LOOPBACK_HOSTS


def _resolvable_key(doc_model: dict) -> str:
    """Any credential this instance can already resolve for the model: the inline doc
    value, the secrets.yaml overlay, or OPENAI_API_KEY — the same three sources
    ``validate_for_headless`` consults. The placeholder does not count as one."""
    import os

    from graph.config_io import load_secrets

    inline = str(doc_model.get("api_key") or "").strip()
    if inline and inline != _LOCAL_KEY_PLACEHOLDER:
        return inline
    overlay = str((load_secrets().get("model") or {}).get("api_key") or "").strip()
    if overlay and overlay != _LOCAL_KEY_PLACEHOLDER:
        return overlay
    return os.environ.get("OPENAI_API_KEY", "").strip()


# HuggingFace passes this literal in the snippet when no specific GGUF file is chosen;
# the local server picks its own default quant, so we just strip it.
_HF_QUANT_PLACEHOLDER = ":{{QUANT_TAG}}"


def _discover(timeout: float = 1.0) -> list[dict]:
    """Probe the well-known local endpoints; return ``[{name, base_url, models}]`` for
    the reachable ones. Best-effort — an unreachable endpoint just isn't listed."""
    import httpx

    out: list[dict] = []
    for name, base in _LOCAL_ENDPOINTS:
        try:
            r = httpx.get(f"{base}/models", timeout=timeout)
            if r.status_code != 200:
                continue
            data = r.json()
            models = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
            out.append({"name": name, "base_url": base, "models": models})
        except Exception:  # noqa: BLE001 — unreachable/parse error ⇒ endpoint not running
            continue
    return out


def _normalize_model_id(model: str) -> str:
    """Tolerate the HF snippet's quant-tag placeholder — HF interpolates the literal
    ``:{{QUANT_TAG}}`` when the user hasn't drilled into a specific GGUF file. Strip it
    (the server defaults to its own quant, e.g. Q4_K_M); a real ``:quant`` is kept."""
    return (model or "").replace(_HF_QUANT_PLACEHOLDER, "").strip()


def _cmd_use(args) -> int:
    from graph.config_io import load_yaml_doc, save_secrets, save_yaml_doc

    model_id = _normalize_model_id(args.model)
    base_url = (args.base_url or "").strip()
    if not base_url or not model_id:
        print("model use: --base-url and --model are required", file=sys.stderr)
        return 2

    doc = load_yaml_doc()
    model = doc.get("model")
    if not isinstance(model, dict):
        model = {}
        doc["model"] = model

    is_local = _is_loopback_endpoint(base_url)
    have_key = _resolvable_key(model)
    warning = ""

    if args.key:
        # A real credential — it belongs in the 0600 overlay, never in the tracked YAML.
        save_secrets({"model": {"api_key": args.key}})
        model.pop("api_key", None)
    elif is_local:
        # Keyless local server: a non-empty placeholder is what the client needs.
        if not str(model.get("api_key") or "").strip() and not have_key:
            model["api_key"] = _LOCAL_KEY_PLACEHOLDER
    elif not have_key:
        # Remote endpoint, no credential anywhere. Do NOT invent one — a placeholder here
        # reads as "configured" to `protoagent setup` and turns a missing key into a 401
        # at the first turn instead of an error at the point of the mistake.
        model.pop("api_key", None)
        warning = (
            f"model use: no API key configured for {base_url} — it is not a local endpoint, so it will\n"
            "  almost certainly reject every request with a 401. Set one with:\n"
            "    protoagent model use --base-url … --model … --key sk-…\n"
            "  (or `protoagent config set model.api_key=sk-…`, or export OPENAI_API_KEY)"
        )

    model["provider"] = args.provider
    model["api_base"] = base_url
    model["name"] = model_id
    save_yaml_doc(doc)

    print(f"model: now {model_id} @ {base_url} (provider={args.provider})")
    if args.key:
        print("key:   stored in config/secrets.yaml (owner-only)")
    if warning:
        print(warning, file=sys.stderr)
    print("Start it with:  protoagent up")
    return 0


def _cmd_discover(_args) -> int:
    found = _discover()
    if not found:
        print(
            "No local OpenAI-compatible endpoint found on the usual ports "
            "(Ollama :11434, LM Studio :1234, llama.cpp/vLLM :8080)."
        )
        print("Start one, or point at any URL:  protoagent model use --base-url <url> --model <id>")
        return 0
    for ep in found:
        print(f"{ep['name']}  {ep['base_url']}")
        for m in ep["models"][:20]:
            print(f"    {m}")
        if len(ep["models"]) > 20:
            print(f"    … +{len(ep['models']) - 20} more")
    print("\nUse one with:  protoagent model use --base-url <url> --model <id>")
    return 0


def _cmd_list(_args) -> int:
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    print(f"current:  {cfg.model_name} @ {cfg.api_base} (provider={cfg.model_provider})")
    found = _discover()
    if found:
        print("\ndiscovered local endpoints:")
        for ep in found:
            print(f"  {ep['name']}  {ep['base_url']}  ({len(ep['models'])} model(s))")
    return 0


def run_model_cli(argv: list[str]) -> int:
    """`protoagent model` — see module docstring. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="protoagent model",
        description="Point protoAgent at a local or OpenAI-compatible LLM.",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<use|discover|list>")

    p_use = sub.add_parser("use", help="Point at an endpoint + model (writes the live config)")
    p_use.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1")
    p_use.add_argument("--model", required=True, help="the model id the endpoint serves")
    p_use.add_argument("--provider", default="openai", help="config provider label (default: openai)")
    p_use.add_argument(
        "--key",
        default="",
        help="API key for the endpoint — stored in config/secrets.yaml, never the tracked config "
        "YAML. Optional only for a keyless server on this machine.",
    )
    p_use.set_defaults(fn=_cmd_use)

    sub.add_parser("discover", help="Probe local endpoints (Ollama / LM Studio / llama.cpp / vLLM)").set_defaults(
        fn=_cmd_discover
    )
    sub.add_parser("list", help="Show the current model + discovered local endpoints").set_defaults(fn=_cmd_list)

    args = parser.parse_args(argv)
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return 0
    return fn(args)
