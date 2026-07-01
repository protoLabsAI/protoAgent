"""The registry handed to a plugin's ``register(registry)`` function.

A plugin contributes capabilities by calling methods on this object; the loader
collects them and threads them into the graph. Keeping the surface small and
explicit means a plugin never imports protoAgent internals to extend it.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("protoagent.plugins")


class PluginRegistry:
    """Collects a single plugin's contributions during ``register()``.

    Contribution types (ADR 0001 + 0018 + 0019):

    - ``tools`` — LangChain ``BaseTool``s (``register_tool[s]``).
    - ``skill_dirs`` — ``SKILL.md`` skill directories (``register_skill_dir``).
    - ``routers`` — FastAPI ``APIRouter``s, mounted under ``/plugins/<id>``
      (``register_router``).
    - ``surfaces`` — lifecycle-managed background surfaces, a ``start`` (+ optional
      ``stop``) run on the server loop (``register_surface``).
    - ``subagents`` — ``SubagentConfig``s added to ``SUBAGENT_REGISTRY``
      (``register_subagent``).
    - ``mcp_servers`` — managed MCP server factories ``config -> entry|None``
      injected into MCP discovery (``register_mcp_server``).
    - ``chat_commands`` — user-only ``/<name>`` control commands that short-circuit
      the turn, like ``/goal`` (``register_chat_command``).

    Routes mount + surfaces start **once** at process init; a config reload reuses
    them — changing ``plugins.enabled`` needs a restart (ADR 0018).
    """

    def __init__(
        self, plugin_id: str, plugin_dir: Path, config: dict | None = None, config_section: str | None = None
    ):
        self.plugin_id = plugin_id
        self.plugin_dir = plugin_dir
        # The plugin's resolved config section (ADR 0019) — manifest defaults ⊕
        # YAML ⊕ secrets. Read it in register() and close over it for your
        # tools/routes/surface, e.g. ``registry.config.get("api_key")``. This is a
        # register-time SNAPSHOT; for a mounted router/surface that must reflect config
        # edits without a restart, use ``live_config()`` instead.
        self.config: dict = dict(config or {})
        # The top-level config key this plugin's section lives under (``config_section``
        # or the id) — the lookup key for ``live_config()``.
        self.config_section: str = config_section or plugin_id
        # Host services (agent invoke + event bus) a surface/route can use — the
        # server populates these before startup; guard for None (e.g. in tests).
        from graph.plugins.host import HOST

        self.host = HOST
        self.tools: list = []
        self.skill_dirs: list[Path] = []
        self.workflow_dirs: list[Path] = []  # dirs of *.yaml workflow recipes (ADR 0027)
        self.a2a_skills: list[dict] = []  # A2A card skill specs (#570)
        self.routers: list[dict] = []  # {"router", "prefix"}
        self.surfaces: list[dict] = []  # {"name", "start", "stop"}
        self.subagents: list = []  # SubagentConfig instances
        self.middleware: list = []  # factories: (config) -> AgentMiddleware|None (ADR 0032)
        self.late_tool_factories: list = []  # factories: (all_tools, config) -> tool|list|None (late seam)
        self.mcp_servers: list = []  # factories: config -> entry dict | None
        self.thread_id_resolver = None  # (request_metadata, session_id) -> str (#571)
        self.goal_verifiers: dict = {}  # name -> async (spec, ctx) -> VerifyResult (ADR 0028)
        self.goal_hooks: list = []  # {on_achieved, on_failed, on_stalled} reactions (ADR 0028 + 0030 D5)
        self.knowledge_stores: dict = {}  # name -> (config) -> KnowledgeBackend (ADR 0031)
        self.embedders: dict = {}  # name -> (config) -> (text -> vector) embed_fn (ADR 0031)
        self.chat_commands: dict = {}  # token -> async (rest, session_id) -> str|None (user-only control commands)

    def live_config(self) -> dict:
        """The plugin's CURRENT resolved config, re-read from the host on each call.

        ``self.config`` is a register-time snapshot, so a hot-reload after a config save
        can't refresh it for an already-mounted router or running surface (FastAPI can't
        re-mount a router). But the reload DOES rebuild ``STATE.graph_config`` — so a
        handler that reads this on each request reflects config edits with no restart.
        Falls back to the snapshot when the host state isn't available (unit tests, or a
        section that resolved empty)."""
        try:
            from runtime.state import STATE

            pconf = getattr(getattr(STATE, "graph_config", None), "plugin_config", None) or {}
            live = pconf.get(self.config_section)
            if isinstance(live, dict):
                return live
        except Exception:  # noqa: BLE001 — best-effort; any failure ⇒ the snapshot
            pass
        return self.config

    def register_tool(self, tool) -> None:
        """Expose a LangChain tool to the agent."""
        if tool is None or not hasattr(tool, "name"):
            log.warning("[plugins] %s: register_tool got a non-tool: %r", self.plugin_id, tool)
            return
        self.tools.append(tool)

    def register_tools(self, tools) -> None:
        """Convenience: register an iterable of tools."""
        for tool in tools or []:
            self.register_tool(tool)

    def register_chat_command(self, name: str, handler) -> None:
        """Own a user-only chat control command — ``/<name> …`` short-circuits the
        turn with the handler's reply, like the core ``/goal`` (and the old, now
        plugin-owned, ``/issue``).

        ``handler`` is ``async (rest: str, session_id: str) -> str | None``: ``rest``
        is everything after the token; return the reply string to send (the turn is
        NOT run through the agent), or ``None`` to pass the message through as a
        normal turn. This is **user-only by design** — it is NOT an agent tool, so a
        plugin can expose a write action (file an issue, open a PR) that the model
        can't invoke autonomously. Read your own config in ``register()`` and close
        over it, e.g. ``repo = self.config.get("default_repo")``.

        The token is slugified + lowercased (``/Issue`` == ``/issue``). The reserved
        core token ``goal`` is refused; a collision with a token another enabled
        plugin already registered keeps the first and warns (resolved in the loader).
        """
        from graph.slash_commands import slugify_slash  # intra-graph, import-safe

        token = slugify_slash(name)
        if not token or not callable(handler):
            log.warning(
                "[plugins] %s: register_chat_command needs a name + callable: %r / %r", self.plugin_id, name, handler
            )
            return
        if token == "goal":
            log.warning("[plugins] %s: chat command /%s is reserved — skipped", self.plugin_id, token)
            return
        if token in self.chat_commands:
            log.warning("[plugins] %s: chat command /%s registered twice — keeping the first", self.plugin_id, token)
            return
        self.chat_commands[token] = handler

    def emit(self, topic: str, data: dict | None = None) -> None:
        """Broadcast an event on the bus (ADR 0039) — fire-and-forget.

        Topics are namespaced to this plugin: ``emit("created")`` publishes
        ``"<plugin_id>.created"``. A topic not already under this plugin's namespace is
        auto-prefixed — a plugin may only publish under its own namespace (the
        no-cross-dependency clause). Consumers subscribe by topic; nobody imports this
        plugin to hear it."""
        pid = self.plugin_id
        if topic != pid and not topic.startswith(f"{pid}."):
            topic = f"{pid}.{topic}"
        if self.host and self.host.publish:
            self.host.publish(topic, data or {})
        else:  # non-server context (tests, headless) — no bus wired
            log.debug("[plugins] %s: emit(%s) dropped — no bus", pid, topic)

    def navigate(self, view: str = "") -> None:
        """Ask the operator console to open one of THIS plugin's views — plugin-driven
        UI navigation (ADR 0044). Fire-and-forget.

        Publishes a reserved host intent ``ui.navigate`` with ``{plugin, view}``; the
        console focuses ``plugin:<this plugin>:<view>`` when that surface exists (a blank
        ``view`` opens the plugin's first view). Unlike :meth:`emit`, this is a host-level
        navigation request, not a namespaced plugin event — and it is **scoped**: the
        payload carries this plugin's id, so a plugin can only open its own views, never
        hijack the console to another surface. The console honors it generically (one
        handler, no per-plugin code), so any plugin gets agent-driven navigation for free.
        """
        if self.host and self.host.publish:
            self.host.publish("ui.navigate", {"plugin": self.plugin_id, "view": view or ""})
        else:  # non-server context (tests, headless) — no bus wired
            log.debug("[plugins] %s: navigate(%s) dropped — no bus", self.plugin_id, view)

    def on(self, topic: str, handler) -> None:
        """Subscribe an in-process handler to bus topics (ADR 0039). ``topic`` may use
        ``*`` (one segment) / ``#`` (tail) wildcards and match ANY plugin's namespace —
        subscribing is read-only and safe. ``handler(payload)`` gets ``{event, data, seq}``,
        may be sync or async; exceptions are isolated by the bus."""
        if not callable(handler):
            log.warning("[plugins] %s: on(%s) needs a callable handler", self.plugin_id, topic)
            return
        if self.host and self.host.on:
            self.host.on(topic, handler)
        else:
            log.debug("[plugins] %s: on(%s) dropped — no bus", self.plugin_id, topic)

    def register_skill_dir(self, path: str | Path) -> None:
        """Add a directory of ``SKILL.md`` skills bundled with the plugin.

        Relative paths resolve against the plugin's own directory.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self.plugin_dir / p
        self.skill_dirs.append(p)

    def register_workflow_dir(self, path: str | Path) -> None:
        """Add a directory of ``*.yaml`` workflow recipes bundled with the plugin
        (ADR 0027). Relative paths resolve against the plugin's own directory. A
        conventional ``<plugin>/workflows/`` dir is auto-discovered without this
        call; use it for a non-standard location.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self.plugin_dir / p
        self.workflow_dirs.append(p)

    def register_goal_verifier(self, name: str, fn) -> None:
        """Contribute an in-process goal verifier (ADR 0028) — an async
        ``(spec, ctx) -> VerifyResult`` referenced by a ``{"type":"plugin",
        "check":"<name>"}`` goal. Name it ``<plugin-id>:<verifier>`` to avoid
        collisions; ``args`` in the spec are declarative data your verifier
        validates (no shell, no eval). This is the only verifier type safe to set
        programmatically (D3)."""
        if not name or not callable(fn):
            log.warning(
                "[plugins] %s: register_goal_verifier needs a name + callable: %r / %r", self.plugin_id, name, fn
            )
            return
        key = name if ":" in name else f"{self.plugin_id}:{name}"
        self.goal_verifiers[key] = fn

    def register_goal_hook(self, *, on_achieved=None, on_failed=None, on_stalled=None) -> None:
        """React when a goal reaches a terminal state (ADR 0028 D4) — or stalls (ADR 0030 D5).
        ``on_achieved`` / ``on_failed`` take the terminal ``GoalState``; ``on_stalled`` (monitor
        goals only) fires when the verifier evidence hasn't moved for ``stall_after`` checks —
        WITHOUT ending the goal, once per stall episode. Each takes the ``GoalState`` (sync or
        async) — push a notification, record a finding, or set the next goal. Provide ANY of the
        three. A raising hook is logged + swallowed."""
        if not (callable(on_achieved) or callable(on_failed) or callable(on_stalled)):
            log.warning(
                "[plugins] %s: register_goal_hook needs on_achieved, on_failed, and/or on_stalled", self.plugin_id
            )
            return
        self.goal_hooks.append(
            {
                "plugin_id": self.plugin_id,
                "on_achieved": on_achieved if callable(on_achieved) else None,
                "on_failed": on_failed if callable(on_failed) else None,
                "on_stalled": on_stalled if callable(on_stalled) else None,
            }
        )

    def register_knowledge_store(self, name: str, factory) -> None:
        """Contribute a knowledge backend (ADR 0031) — ``factory(config) ->
        KnowledgeBackend`` (see ``knowledge.backend.KnowledgeBackend`` for the
        surface: pgvector, Qdrant, Chroma, a managed vector DB…). Selected by a
        fork with ``knowledge.backend: "<name>"``; on a None/error return the agent
        keeps the built-in SQLite store (degrade-safe). Name it simply (e.g.
        ``pgvector``); a collision keeps the first."""
        if not name or not callable(factory):
            log.warning(
                "[plugins] %s: register_knowledge_store needs a name + factory: %r / %r", self.plugin_id, name, factory
            )
            return
        self.knowledge_stores[name] = factory

    def register_embedder(self, name: str, factory) -> None:
        """Contribute an in-process embedder (ADR 0031 follow-up) — ``factory(config)
        -> (text: str) -> list[float]``. Selected by a fork with
        ``knowledge.embedder: "<name>"`` for the built-in hybrid store, avoiding the
        gateway round-trip (e.g. fastembed / sentence-transformers). On a None/error
        return the agent falls back to the gateway embedder (degrade-safe)."""
        if not name or not callable(factory):
            log.warning(
                "[plugins] %s: register_embedder needs a name + factory: %r / %r", self.plugin_id, name, factory
            )
            return
        self.embedders[name] = factory

    def register_a2a_skill(self, spec: dict) -> None:
        """Contribute an A2A *card* skill — advertised on the agent card and,
        when it declares ``output_schema`` + ``result_mime``, enforced by the
        executor's structured finalizer (#570). Distinct from
        ``register_skill_dir`` (disk ``SKILL.md`` procedural memory): this is what
        the card advertises to callers. ``spec`` is a dict with at least
        ``id``/``name``/``description`` (+ optional ``tags``/``examples``/
        ``output_schema``/``result_mime``)."""
        if not isinstance(spec, dict) or not spec.get("id") or not spec.get("name"):
            log.warning("[plugins] %s: register_a2a_skill needs a dict with id+name: %r", self.plugin_id, spec)
            return
        self.a2a_skills.append(spec)

    def register_thread_id_resolver(self, fn) -> None:
        """Override how the checkpointer ``thread_id`` is derived for each turn
        (#571): ``fn(request_metadata: dict, session_id: str) -> str``. Lets a
        fork scope memory off request metadata (e.g. per-project working memory)
        without editing ``server/chat.py``. One resolver wins — last registration
        across enabled plugins (a warning fires if more than one is contributed).
        Unset ⇒ the template default (``a2a:<session_id>``)."""
        if not callable(fn):
            log.warning("[plugins] %s: register_thread_id_resolver needs a callable: %r", self.plugin_id, fn)
            return
        self.thread_id_resolver = fn

    def register_router(self, router, prefix: str | None = None) -> None:
        """Mount a FastAPI ``APIRouter`` on the server (ADR 0018).

        Defaults to the namespaced prefix ``/plugins/<id>`` so a plugin can't
        silently shadow a core route. Pass ``prefix=""`` (or your own) to mount
        elsewhere — an escape hatch, logged. Plugin routes SHOULD live under
        ``/plugins/<id>/``; a non-conforming prefix logs a WARNING (#870).
        The default-deny auth middleware guards all non-public paths regardless
        of prefix.
        """
        if router is None or not hasattr(router, "routes"):
            log.warning("[plugins] %s: register_router got a non-router: %r", self.plugin_id, router)
            return
        eff = f"/plugins/{self.plugin_id}" if prefix is None else str(prefix)
        if eff and not eff.startswith(f"/plugins/{self.plugin_id}"):
            log.warning(
                "[plugins] %s: register_router prefix %r does not start with "
                "/plugins/%s/ — plugin routes SHOULD live under /plugins/<id>/",
                self.plugin_id,
                eff,
                self.plugin_id,
            )
        self.routers.append({"router": router, "prefix": eff})

    def register_surface(self, start, stop=None, name: str | None = None, reload=None) -> None:
        """Register a lifecycle-managed background surface (ADR 0018).

        ``start`` (sync or async, no args) runs in the server's startup hook — so
        it has the running loop, like the Discord gateway — and may return a task/
        handle. ``stop`` (optional) runs in shutdown. ``reload`` (optional, called
        with the new ``LangGraphConfig`` on a config reload) lets a surface
        reconnect when its config changes — without it, surfaces wire once and a
        config change needs a restart. Best-effort: a failing surface logs, never
        breaks boot.
        """
        if not callable(start):
            log.warning("[plugins] %s: register_surface needs a callable start", self.plugin_id)
            return
        self.surfaces.append({"name": name or self.plugin_id, "start": start, "stop": stop, "reload": reload})

    def register_mcp_server(self, factory) -> None:
        """Contribute a **managed MCP server** the agent connects to (ADR 0019).

        ``factory`` is a callable ``factory(config) -> dict | None`` returning a
        ``mcp.servers[]`` entry (``{name, transport, command, args, env, ...}``) or
        ``None`` when the server shouldn't start (off / not yet connected). It's
        called at every graph build with the live ``LangGraphConfig``, so the
        server comes and goes with config — this is how a plugin ships an
        OAuth-gated MCP server without a core edit. A returned entry whose
        ``name`` matches a user-defined ``mcp.servers`` entry replaces it.
        """
        if not callable(factory):
            log.warning("[plugins] %s: register_mcp_server needs a callable", self.plugin_id)
            return
        self.mcp_servers.append(factory)

    def register_subagent(self, config) -> None:
        """Add a ``SubagentConfig`` to ``SUBAGENT_REGISTRY`` (ADR 0018).

        Picked up by every graph build, so the lead agent can delegate to it via
        ``task`` / ``task_batch`` — no edit to ``graph/subagents/config.py``.
        """
        if config is None or not getattr(config, "name", None):
            log.warning("[plugins] %s: register_subagent got an invalid config: %r", self.plugin_id, config)
            return
        self.subagents.append(config)

    def register_middleware(self, factory) -> None:
        """Add a plugin-contributed LangGraph ``AgentMiddleware`` (ADR 0032).

        ``factory`` is ``(config) -> AgentMiddleware | None`` — it receives the live
        ``LangGraphConfig`` and returns a middleware instance (or None to opt out).
        Plugin middleware is appended to the chain just before the internal
        message-capture middleware (so before/after-model + tool hooks run, and the
        turn is still captured). For per-request data, read
        ``graph.middleware.request_context.current_request_metadata()``.

        This is the last core extension point that previously forced a fork to edit
        ``graph/agent.py`` / ``executor.py``.
        """
        if not callable(factory):
            log.warning("[plugins] %s: register_middleware needs a callable factory, got %r", self.plugin_id, factory)
            return
        self.middleware.append(factory)

    def register_late_tool_factory(self, factory) -> None:
        """Contribute a tool factory that runs AFTER the full toolset is assembled.

        ``factory(all_tools, config) -> BaseTool | list[BaseTool] | None`` receives the
        fully-resolved tool list (core + subagent + plugin + MCP tools) and the live
        ``LangGraphConfig``; its result is appended last, before the deferred
        ``search_tools`` meta-tool (so the late tool stays discoverable). For a
        *meta-tool* that must see or wrap **every** other tool — e.g. programmatic
        tool-calling that proxies the whole set — which a plain ``register_tool`` can't,
        because plugin tools are registered before the set is complete. Built last, so
        it can reference any tool but never itself. A raising factory is logged and
        skipped, never breaking the graph build.
        """
        if not callable(factory):
            log.warning("[plugins] %s: register_late_tool_factory needs a callable, got %r", self.plugin_id, factory)
            return
        self.late_tool_factories.append(factory)
