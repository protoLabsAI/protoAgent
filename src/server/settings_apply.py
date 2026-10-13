"""Settings apply: the config write + reload path the operator console, the setup wizard,
the plugin host and the devkit drive.

Extracted from ``server/agent_init.py`` (#3848, epic #3804). This module owns:

- ``_serialized_config_write`` — the ``CONFIG_WRITE_LOCK`` decorator (the lock itself lives
  in ``graph.config_io``; agent_init's ``_reload_langgraph_agent`` is decorated with it too).
- ``_apply_settings_changes`` / ``_reset_settings_keys`` — validate → persist → reload, with
  the host-layer filter (``_filter_nested_to_host_keys`` / ``_prune_shadowing_agent_keys``)
  and the failed-reload rollback (``_snapshot_config_files`` / ``_restore_config_files`` /
  ``_drop_undone_write_messages``).
- ``_sync_autostart_with_config`` — the OS autostart side effect shared by both save paths.
- ``_build_settings_callbacks`` — the console Settings + setup-wizard callbacks
  (``finish_setup`` included).

``server.agent_init`` (and ``server/__init__``) re-export every name here by identity.

**Where to patch.** What these functions call by bare name — ``_sync_autostart_with_config``,
the snapshot/restore helpers, ``_event_bus`` — resolves in THIS module's globals: patch it
here, not on ``agent_init``. The two exceptions are agent_init seams, called through the
module at call time so a patch on ``agent_init`` still intercepts them:
``_reload_langgraph_agent`` (stays in agent_init) and ``_apply_settings_changes`` itself.
Its published address stays ``server.agent_init._apply_settings_changes``: operator_api
and the devkit plugin import it from there at call time (the layering contracts bind them
to that one module), maintenance_loops / plugin_wiring / agent_init's plugin host call it
through agent_init, and ``save_all`` below does too — so ONE patch point intercepts every
caller. (``server/__init__`` re-exports it too, but only as a name: nothing calls it
through ``server``, so a patch there intercepts nothing — tests/test_settings_apply_seam.py
pins both halves.)
"""

import functools
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.state import STATE
from server import _event_bus

log = logging.getLogger("protoagent.server")

# Serializes every config read-modify-write + graph reload. The callers all
# run on worker threads (asyncio.to_thread from the routes), and a fleet hub
# makes concurrent saves routine (two console windows, a settings save racing
# a plugin toggle). Without this: classic lost-update on the YAML, and two
# interleaved reloads can commit graph A with STATE.graph_config B — the exact
# de-sync the reload path's build-then-commit choreography assumes can't
# happen. RLock because _apply_settings_changes/_reset_settings_keys call
# _reload_langgraph_agent, which is also lockable on its own (plugin routes
# call it directly). It is graph.config_io.CONFIG_WRITE_LOCK — defined with the
# writes so the graph layer's own config writers share it (#2743).
from graph.config_io import CONFIG_WRITE_LOCK as _CONFIG_WRITE_LOCK  # noqa: E402


def _agent_init():
    """``server.agent_init``, resolved at call time — it imports this module at load, so a
    top-level import here would be a cycle, and a call-time lookup is what lets a test's
    patch on an agent_init seam (``_reload_langgraph_agent``, ``_apply_settings_changes``)
    intercept the callers below."""
    from server import agent_init

    return agent_init


def _serialized_config_write(fn):
    """Run ``fn`` under ``_CONFIG_WRITE_LOCK`` (config RMW + reload guard)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _CONFIG_WRITE_LOCK:
            return fn(*args, **kwargs)

    return wrapper


def _sync_autostart_with_config(config: dict | None) -> str | None:
    """Align the OS autostart artifact with the YAML runtime flag.

    Returns a short status string to append to the caller's message
    log, or ``None`` when the config doesn't touch the runtime
    section. Shared by ``finish_setup`` (wizard path) and
    ``_apply_settings_changes`` (drawer path) so both surfaces
    produce the same side effect when the checkbox flips.
    """
    if not (config and "runtime" in config):
        return None
    want = bool(config.get("runtime", {}).get("autostart_on_boot", False))

    try:
        from infra.autostart import install_autostart, uninstall_autostart

        as_name = (
            config.get("identity", {}).get("name")
            or (STATE.graph_config.identity_name if STATE.graph_config else "")
            or "protoagent"
        )
        if want:
            ok, msg = install_autostart(agent_name=as_name, port=STATE.active_port)
        else:
            ok, msg = uninstall_autostart(agent_name=as_name)
    except Exception as e:
        log.exception("[autostart] sync raised")
        return f"autostart failed: {e}"

    if not ok:
        log.warning("[autostart] sync failed: %s", msg)
    return f"autostart: {msg}"


def _filter_nested_to_host_keys(config: dict) -> tuple[dict, list[str]]:
    """Keep only host-scoped (ADR 0047 ``scope=="host"``) leaves of a nested config
    dict; return ``(host_only, dropped)`` where ``dropped`` is the dotted keys that
    were not host-scoped (agent-only / secret) and so are refused on the Host layer.

    Mirrors ``graph.config_load._filter_to_host_keys`` (the READ-side guard) so the host
    file can't accumulate agent keys, and enforces D5: secret-typed keys are never
    written to the non-secret host file."""
    from graph.config import _get_dotted, _set_dotted
    from graph.settings_schema import host_keys, is_secret_key

    allowed = host_keys()
    host_only: dict = {}
    dropped: list[str] = []

    def _walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _walk(v, dotted)
                continue
            if dotted in allowed and not is_secret_key(dotted):
                found, val = _get_dotted(config, dotted)
                if found:
                    _set_dotted(host_only, dotted, val)
            else:
                dropped.append(dotted)

    _walk(config, "")
    return host_only, dropped


def _prune_shadowing_agent_keys(host_only: dict) -> list[str]:
    """Delete the just-saved host-scoped keys from the AGENT leaf so the host
    default actually wins.

    A host-console save writes the box-shared host file, but the agent leaf
    (``langgraph-config.yaml``) sits ABOVE the host layer in the ADR 0047
    cascade (agent > host > app). An agent-layer copy of the same key — almost
    always an unmodified seed from ``langgraph-config.example.yaml`` — silently
    shadows the host value the operator just set on the Host console, so the
    edit appears to "reset". On a host save we therefore remove those keys from
    the agent leaf; the effective value then resolves from the host file.

    ``host_only`` is the nested dict of host-scoped leaves written to the host
    file. Returns the dotted paths cleared (for the operator message); empty
    when nothing shadowed."""
    import graph.config_io as _cio
    from graph.config_io import load_yaml_doc, save_yaml_doc

    leaf = _cio.config_yaml_path()  # resolved at call time (honors a repoint)
    if not Path(leaf).exists():
        return []  # no agent leaf on disk → nothing can shadow; don't seed one
    doc = load_yaml_doc(leaf)
    removed: list[str] = []

    def _walk(node: Any, keys: Any, prefix: str) -> None:
        if not isinstance(node, dict) or not isinstance(keys, dict):
            return
        for k, v in list(keys.items()):
            if k not in node:
                continue
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and isinstance(node.get(k), dict):
                _walk(node[k], v, dotted)
                if not node[k]:  # parent map emptied by the prune → drop it too
                    del node[k]
            else:
                del node[k]
                removed.append(dotted)

    _walk(doc, host_only, "")
    if removed:
        save_yaml_doc(doc, leaf)
    return removed


def _config_files_to_snapshot() -> list[Path]:
    """Every file a settings write can touch — the agent leaf, the untracked secrets
    sibling, and the box-shared host layer. Resolved at call time (each honours an
    instance repoint / test monkeypatch). A path that can't resolve is skipped, not fatal:
    the snapshot is a safety net, and half a net still catches the file that changed."""
    import graph.config_io as _cio

    out: list[Path] = []
    for resolve in (
        _cio.config_yaml_path,
        _cio.secrets_yaml_path,
        lambda: __import__("infra.paths", fromlist=["host_config_path"]).host_config_path(),
    ):
        try:
            p = Path(resolve())
        except Exception:  # noqa: BLE001 — an unresolvable layer just isn't snapshotted
            log.debug("[config] snapshot: a config path did not resolve", exc_info=True)
            continue
        if p not in out:
            out.append(p)
    return out


def _snapshot_config_files() -> dict[Path, tuple[bytes, int] | None]:
    """Byte+mode snapshot of the config files, for rolling back a write whose reload fails.

    ``None`` records "this file did not exist" so a restore deletes a file the write
    created (e.g. a first-ever secrets.yaml). Mode is captured so restoring the 0600
    secrets file can't silently widen its permissions.
    """
    snaps: dict[Path, tuple[bytes, int] | None] = {}
    for p in _config_files_to_snapshot():
        try:
            st = p.stat()
            snaps[p] = (p.read_bytes(), st.st_mode)
        except FileNotFoundError:
            snaps[p] = None
        except OSError:
            # Unreadable → we can't promise to restore it, so don't pretend we can.
            log.warning("[config] snapshot: %s unreadable; not covered by rollback", p)
    return snaps


def _restore_config_files(snaps: dict[Path, tuple[bytes, int] | None]) -> list[str]:
    """Put the config files back exactly as ``_snapshot_config_files`` found them.

    Returns the paths actually rewritten (empty = the write never landed, nothing to undo).
    Best-effort per file: a restore that itself fails is logged loudly rather than raised —
    the caller is already on a failure path, and one unrestorable file must not stop the
    others going back.
    """
    restored: list[str] = []
    for p, snap in snaps.items():
        try:
            if snap is None:
                if p.exists():
                    p.unlink()
                    restored.append(str(p))
                continue
            data, mode = snap
            if p.exists() and p.read_bytes() == data:
                continue  # untouched by this write
            p.write_bytes(data)
            os.chmod(p, mode & 0o7777)  # a restored secrets.yaml keeps its 0600
            restored.append(str(p))
        except OSError:
            log.exception("[config] ROLLBACK FAILED for %s — disk may not match the running agent", p)
    return restored


# A reload that returns False has committed NOTHING (see _reload_langgraph_agent: both of its
# failure exits — "config load failed" and "graph rebuild failed" — return before
# `STATE.graph = new_graph`, and the rebuild path closes the MCP clients it built). The old
# graph is still serving. So the honest response to a failed reload is to put the YAML back:
# otherwise disk says one thing, the running agent does another, and the NEXT restart boots the
# config the rebuild just rejected. That is not theoretical — an ACP-only instance (no gateway
# key: create_llm's ACP fallback covers acp:*, but `native` needs a real key) could be made
# unbootable by one dropdown change, recoverable only by hand-editing YAML.
_ROLLBACK_NOTE = "rolled back — your config and the running agent are unchanged"

# Messages that a rollback makes retroactively false: the write they announce has been undone,
# so reporting "config saved · rebuild failed · rolled back" tells the operator their config
# both did and didn't save. Dropped from the response when the undo lands.
_WRITE_ANNOUNCEMENTS = ("config saved", "host config saved")


def _drop_undone_write_messages(messages: list[str]) -> list[str]:
    """Strip the now-false "saved" lines after a rollback, keeping the failure reason."""
    return [
        m for m in messages if not (m in _WRITE_ANNOUNCEMENTS or m.startswith("reset ") and m.endswith("to inherited"))
    ]


@_serialized_config_write
def _apply_settings_changes(
    config: dict | Callable[[Any], dict | None] | None = None,
    soul: str | None = None,
    layer: str = "agent",
) -> tuple[bool, list[str]]:
    """Persist config YAML + SOUL.md then reload the graph once.

    Passing ``None`` for either argument skips that write — a bare
    call with both None acts as a pure reload (useful for picking up
    external file edits).

    ``config`` may be a CALLABLE ``(current_config) -> updates`` for a
    read-modify-write (#2743). It runs here, INSIDE ``_CONFIG_WRITE_LOCK``,
    against ``STATE.graph_config`` — which every locked write commits before
    releasing — so the merge sees every earlier writer's change. Computing the
    merge from a config read BEFORE the lock is the lost update: two
    concurrent plugin installs both read ``[a]``, write ``[a, x]`` and
    ``[a, y]``, and ``x``'s enable silently vanishes. Returning ``None`` makes
    it a pure reload.

    ``layer`` selects the cascade file the config write lands in (ADR 0047 slice 3):

    * ``"agent"`` (default) — TODAY's exact behavior: write the agent leaf
      ``langgraph-config.yaml`` (secrets split out to the sibling ``secrets.yaml``).
    * ``"host"`` — write the box-shared host file (``paths.host_config_path()``),
      filtered to host-scoped FIELDS keys only. No secrets land on the host layer
      (D5): a secret-typed key is refused. SOUL writes are agent-local regardless.
    """
    from graph.config_io import (
        apply_updates_to_yaml,
        load_yaml_doc,
        save_secrets,
        save_yaml_doc,
        split_secret_updates,
        strip_secrets_from_doc,
        validate_config_dict,
        write_soul,
    )

    messages: list[str] = []
    if callable(config):
        try:
            config = config(STATE.graph_config)
        except Exception as e:  # noqa: BLE001 — keep the (ok, messages) contract
            # Nothing is written yet, so there is nothing to roll back. Raising instead
            # would turn e.g. an install whose code is already on disk into a bare 500,
            # where the caller's contract is "installed; enabling failed: <why>".
            log.exception("[config] computing the config update failed")
            return False, [f"config update: {e}"]
    # Snapshot BEFORE the first write so a failed reload can undo it (see _ROLLBACK_NOTE).
    # Only when there's a config write to undo: a pure reload / SOUL-only save has no YAML
    # change to revert, and SOUL is deliberately outside the rollback — it's authored prose,
    # and silently reverting an operator's persona edit because a plugin failed to rebuild
    # would destroy work to fix a mismatch that the persona didn't cause.
    snaps = _snapshot_config_files() if config is not None else None

    if config is not None:
        ok, err = validate_config_dict(config)
        if not ok:
            return False, [f"validation: {err}"]
        if layer == "host":
            try:
                from infra.paths import host_config_path

                host_only, dropped = _filter_nested_to_host_keys(config)
                if dropped:
                    messages.append(f"host layer: ignored non-host key(s) {', '.join(sorted(dropped))}")
                hp = host_config_path()
                doc = load_yaml_doc(hp)
                apply_updates_to_yaml(doc, host_only)
                strip_secrets_from_doc(doc)  # belt-and-suspenders: never a secret on host
                save_yaml_doc(doc, hp)
                messages.append("host config saved")
                # The agent leaf outranks the host file (ADR 0047 agent > host),
                # so a leftover agent-layer copy of the same key would shadow the
                # value just set — clear it so the host default takes effect.
                cleared = _prune_shadowing_agent_keys(host_only)
                if cleared:
                    messages.append(
                        "cleared shadowing agent override(s) so the host value wins: " + ", ".join(sorted(cleared))
                    )
            except Exception as e:
                log.exception("[config] host YAML write failed")
                return False, [f"host config write: {e}"]
        else:
            try:
                import graph.config_io as _cio

                main_config, secret_updates = split_secret_updates(config)
                save_secrets(secret_updates)
                leaf = _cio.config_yaml_path()  # resolved at call time (honors a repoint)
                doc = load_yaml_doc(leaf)
                apply_updates_to_yaml(doc, main_config)
                strip_secrets_from_doc(doc)
                save_yaml_doc(doc, leaf)
                messages.append("config saved")
            except Exception as e:
                log.exception("[config] YAML write failed")
                return False, [f"config write: {e}"]

    if soul is not None:
        try:
            paths = write_soul(soul)
            messages.append(f"SOUL saved ({len(paths)} path{'s' if len(paths) != 1 else ''})")
        except Exception as e:
            log.exception("[config] SOUL write failed")
            return False, [f"soul write: {e}"]

    # Drawer toggles of runtime.autostart_on_boot ride this path,
    # not the wizard's finish_setup, so the LaunchAgent plist has
    # to be installed/removed here too. runtime.* is agent-scoped, so this
    # only fires on the agent layer (a host write never carries it).
    if layer != "host":
        as_msg = _sync_autostart_with_config(config)
        if as_msg:
            messages.append(as_msg)

    ok, reload_msg = _agent_init()._reload_langgraph_agent()
    messages.append(reload_msg)
    if not ok and snaps is not None:
        if _restore_config_files(snaps):
            messages = _drop_undone_write_messages(messages)
            messages.append(_ROLLBACK_NOTE)
            # _sync_autostart_with_config already ran against the now-reverted patch, so a
            # rejected save could otherwise leave a LaunchAgent plist installed (or removed)
            # for a config that no longer exists on disk. Re-sync to the value we rolled back
            # TO — STATE.graph_config is still the old config precisely because the rebuild
            # never committed. Only when this save actually touched runtime.*.
            if layer != "host" and config and "runtime" in config:
                try:
                    _sync_autostart_with_config(
                        {
                            "runtime": {
                                "autostart_on_boot": bool(
                                    getattr(STATE.graph_config, "runtime_autostart_on_boot", False)
                                )
                            }
                        }
                    )
                except Exception:  # noqa: BLE001 — never mask the original failure
                    log.exception("[config] autostart re-sync after rollback failed")
    if ok and (config is None or "plugins" in config):
        # ADR 0096 D8: a plugin-state change must reach EVERY open console, not just
        # the tab that clicked — console mutations invalidate their own queries, but
        # the devkit tools (agent-initiated enable/reload) and autoupdate have no tab
        # at all. One publish on the reload seam covers all callers; the console's
        # PluginChangeWatch subscribes to `plugin.#`. A bare call is a pure reload
        # (plugins re-exec), a `plugins` key is an enable/disable — other settings
        # saves don't change plugin state and stay quiet. Guarded: a bus hiccup must
        # never fail a save (same posture as the autoupdate publish).
        try:
            _event_bus.publish(
                "plugin.changed",
                {"scope": "plugins" if config is not None else "reload"},
            )
        except Exception:  # noqa: BLE001
            log.exception("[config] plugin.changed publish failed")
    return ok, messages


@_serialized_config_write
def _reset_settings_keys(keys: list[str]) -> tuple[bool, list[str]]:
    """Reset-to-inherited (ADR 0047 slice 3): pop ``keys`` from the AGENT leaf
    YAML, then reload so each field falls back to the Host/App layer.

    Always operates on the leaf (the layer the settings UI edits per-agent); the
    Host file is left untouched, so resetting an agent override surfaces the host
    default. A pure reload when ``keys`` is empty."""
    import graph.config_io as _cio
    from graph.config_io import load_yaml_doc, pop_keys_from_yaml, save_yaml_doc

    messages: list[str] = []
    # Same contract as _apply_settings_changes: a reload that fails committed nothing, so the
    # popped keys go back rather than leaving disk ahead of the running agent. Dropping an
    # override can make a graph unbuildable just as easily as setting one (the inherited value
    # is what gets rebuilt against).
    snaps = _snapshot_config_files() if keys else None
    if keys:
        try:
            leaf = _cio.config_yaml_path()  # resolved at call time (honors a repoint)
            doc = load_yaml_doc(leaf)
            pop_keys_from_yaml(doc, keys)
            save_yaml_doc(doc, leaf)
            messages.append(f"reset {len(keys)} key(s) to inherited")
        except Exception as e:
            log.exception("[config] reset (pop keys) failed")
            return False, [f"reset: {e}"]

    ok, reload_msg = _agent_init()._reload_langgraph_agent()
    messages.append(reload_msg)
    if not ok and snaps is not None and _restore_config_files(snaps):
        messages = _drop_undone_write_messages(messages)
        messages.append(_ROLLBACK_NOTE)
    return ok, messages


def _build_settings_callbacks() -> dict[str, Any]:
    """Callbacks consumed by the console Settings (config routes) + the setup wizard."""
    from graph.config import resolve_model_route
    from graph.config_io import (
        config_to_dict,
        is_setup_complete,
        list_available_tools,
        list_gateway_models,
        list_soul_presets,
        mark_setup_complete,
        read_soul,
        read_soul_preset,
        reset_setup,
    )

    def get_config() -> dict[str, Any]:
        return config_to_dict(STATE.graph_config)

    def list_models(api_base: str = "", api_key: str = "") -> tuple[list[str], str]:
        """UI-friendly model lookup.

        Uses the form-local api_base/api_key when the user is trying a
        different endpoint before saving; falls back to the currently
        loaded graph config so the initial render works without
        arguments.
        """
        live = resolve_model_route(STATE.graph_config) if STATE.graph_config else None
        base = api_base or (live.base_url if live else "")
        key = api_key or (live.api_key if live else "")
        return list_gateway_models(base, key)

    def save_all(config: dict | None, soul: str | None) -> tuple[bool, str]:
        ok, messages = _agent_init()._apply_settings_changes(config=config, soul=soul)
        return ok, " • ".join(messages)

    def finish_setup(config: dict | None, soul: str | None) -> tuple[bool, str]:
        """Wizard terminal action — write everything, mark complete, reload.

        Ordering matters:

        1. Write config YAML + SOUL.md (no reload yet).
        2. ``mark_setup_complete()`` — flip the marker BEFORE the
           reload so ``_reload_langgraph_agent`` actually compiles
           the graph. Doing it after means the reload sees
           setup-incomplete and stays ``STATE.graph = None``.
        3. Sync autostart (LaunchAgent plist is independent of the
           graph, so it can happen any time after the config is
           written).
        4. Reload — marker present, graph compiles, chat works.

        Returns a single status string joining per-step messages.
        """
        from graph.config_io import (
            apply_updates_to_yaml,
            load_yaml_doc,
            save_secrets,
            save_yaml_doc,
            split_secret_updates,
            strip_secrets_from_doc,
            validate_config_dict,
            validate_model_connection,
            write_soul,
        )

        messages: list[str] = []

        # 0. Verify the model can actually complete BEFORE we touch anything —
        # otherwise the graph compiles fine but every chat 401s, with no UI
        # signal (the bug that motivated this gate). A real 1-token completion
        # exercises the same auth path as chat, so a bad key / wrong model /
        # unreachable gateway is caught here and returned to the wizard verbatim
        # (e.g. "expected to start with 'sk-'"). Setup stays incomplete, so the
        # operator fixes it in the UI and retries — no file editing required.
        # …unless the runtime is ACP (acp:<agent>): the coding agent is the brain and
        # may have no gateway key at all (ADR 0033). Probing a gateway we won't use would
        # wrongly block setup, so skip it — the model block is still persisted for native
        # delegates/fallback if the operator filled it in. Native OAuth providers
        # (anthropic-oauth / openai-codex, ADR 0097) are the same: they authenticate from a
        # credential store, not api_base/key, so this gateway probe would fall back to the
        # SAVED gateway base + a Claude/Codex model and 401 ("No api key passed in"). The
        # wizard's own Test-connection button covers the real OAuth check.
        from graph.providers import is_native_oauth_provider

        _runtime = str((config or {}).get("agent_runtime", "native") or "native")
        _model_cfg = (config or {}).get("model")
        # Setup writes ONE connection now (ADR 0106) and a qualified `model.name`, so the
        # probe reads the connection rather than the retired provider/base/key triple.
        # Older clients still send the triple; both shapes are accepted here because this
        # runs before anything is persisted, and a first run that cannot be probed is a
        # first run that cannot complete.
        _conns = (config or {}).get("providers")
        _conns = [c for c in _conns if isinstance(c, dict)] if isinstance(_conns, list) else []
        # Resolve the connection `model.name` actually NAMES, not simply the first one:
        # probing connection #1 while the model runs on #2 tests the wrong endpoint and
        # leaves the prefix unstripped. The wizard writes exactly one today, so this is
        # about any other caller of finish_setup — and about not encoding "there is only
        # one" a second time, which is the assumption this whole change removes.
        _named = str(((config or {}).get("model") or {}).get("name", "") or "").partition(":")[0].strip().lower()
        _conn = next((c for c in _conns if str(c.get("id", "") or "").strip().lower() == _named), None)
        if _conn is None:
            _conn = _conns[0] if _conns else None
        _provider = str((_conn or {}).get("type", "") or "") or (
            str((_model_cfg or {}).get("provider", "") or "") if isinstance(_model_cfg, dict) else ""
        )
        _skip_probe = _runtime.startswith("acp:") or is_native_oauth_provider(_provider)
        if not _skip_probe and config is not None and isinstance(_model_cfg, dict):
            m = _model_cfg
            live = resolve_model_route(STATE.graph_config) if STATE.graph_config else None
            if _conn is not None:
                # ADR 0106 (#3128): a DECLARED connection is probed strictly from its own
                # fields — never `model.api_key`, never the live route, never
                # OPENAI_API_KEY. Falling through to any of those sends one connection's
                # credential to another connection's endpoint. A blank key here means this
                # endpoint needs none (a local vLLM or Ollama), not "borrow the global one".
                test_base = str(_conn.get("base_url") or "")
                test_key = str(_conn.get("api_key") or "")
                allow_env = False
            else:
                # No registry entry: the pre-0106 triple, resolved as it always was.
                test_base = m.get("api_base") or (live.base_url if live else "")
                test_key = m.get("api_key") or (live.api_key if live else "")
                allow_env = True
            test_model = m.get("name") or (STATE.graph_config.model_name if STATE.graph_config else "")
            # The gateway is asked for a MODEL, not for a route: sending it
            # `gateway:protolabs/reasoning` verbatim probes a model id that does not exist.
            if _conn and isinstance(test_model, str):
                prefix = f"{_conn.get('id', '')}:"
                if prefix != ":" and test_model.startswith(prefix):
                    test_model = test_model[len(prefix) :]
            ok, verr = validate_model_connection(test_base, test_key, test_model, allow_env_key=allow_env)
            if not ok:
                return False, f"model connection failed — {verr}"

        # 1. Persist (secrets to the untracked overlay, never the tracked YAML)
        if config is not None:
            ok, err = validate_config_dict(config)
            if not ok:
                return False, f"validation: {err}"
            try:
                main_config, secret_updates = split_secret_updates(config)
                save_secrets(secret_updates)
                doc = load_yaml_doc()
                apply_updates_to_yaml(doc, main_config)
                strip_secrets_from_doc(doc)
                save_yaml_doc(doc)
                messages.append("config saved")
            except Exception as e:
                log.exception("[setup] YAML write failed: %s", e)
                return False, f"config write: {e}"

        if soul is not None:
            try:
                paths = write_soul(soul)
                messages.append(f"SOUL saved ({len(paths)} path{'s' if len(paths) != 1 else ''})")
            except Exception as e:
                log.exception("[setup] SOUL write failed: %s", e)
                return False, f"soul write: {e}"

        # 2. Flip the marker — MUST be before reload so the graph builds
        mark_setup_complete()
        messages.append("setup marked complete")

        # 3. Autostart sync (shared helper — drawer path runs the same)
        as_msg = _sync_autostart_with_config(config)
        if as_msg:
            messages.append(as_msg)

        # 4. Reload — now picks up setup_complete=True and compiles.
        # On failure, roll back the marker so the next page load
        # drops the user back into the wizard instead of landing
        # them in the chat UI with the "setup required" fallback
        # and no obvious way to retry.
        ok, reload_msg = _agent_init()._reload_langgraph_agent()
        messages.append(reload_msg)
        if not ok:
            reset_setup()
            messages.append("setup marker rolled back — re-run the wizard after fixing the error above")

        return ok, " • ".join(messages)

    def restart_setup() -> str:
        """Drawer action — delete the marker so the wizard runs again."""
        reset_setup()
        log.info("[setup] marker removed — wizard will run on next page load")
        return "setup marker removed • reload the page to run the wizard"

    def autostart_info() -> dict[str, Any]:
        """Report platform support + current on-disk state. The drawer
        uses this to render the toggle correctly and to print the
        plist path for debugging."""
        try:
            from infra.autostart import autostart_status

            name = (STATE.graph_config.identity_name if STATE.graph_config else "") or "protoagent"
            return autostart_status(name)
        except Exception as e:
            return {"supported": False, "installed": False, "reason": str(e)}

    def toggle_autostart(enabled: bool) -> tuple[bool, str]:
        """Install or uninstall the OS autostart artifact, mirroring
        the YAML field. Called from the drawer's checkbox handler so
        toggling takes effect immediately without waiting for Save."""
        try:
            from infra.autostart import install_autostart, uninstall_autostart

            name = (STATE.graph_config.identity_name if STATE.graph_config else "") or "protoagent"
            if enabled:
                return install_autostart(agent_name=name, port=STATE.active_port)
            return uninstall_autostart(agent_name=name)
        except Exception as e:
            return False, str(e)

    return {
        "get_config": get_config,
        "get_soul": read_soul,
        "list_models": list_models,
        "list_tools": list_available_tools,
        "list_soul_presets": list_soul_presets,
        "read_soul_preset": read_soul_preset,
        "save_all": save_all,
        "finish_setup": finish_setup,
        "restart_setup": restart_setup,
        "is_setup_complete": is_setup_complete,
        "autostart_info": autostart_info,
        "toggle_autostart": toggle_autostart,
    }
