"""
Reusable Gradio chat UI for protoAgent.

Provides a clean dark-themed chat interface. Includes a settings
sidebar with model/tools panels. Rename the CSS constants below if
your fork wants a different accent palette.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import gradio as gr

CLEAN_CSS = """
    footer { display: none !important; }
    .prose { overflow: hidden !important; max-height: 3em !important; }
    .built-with { display: none !important; }
    button.copy-btn, button.like, button.dislike,
    .message-buttons-left, .message-buttons-right,
    .bot .message-buttons, .user .message-buttons,
    .copy-button, .action-button,
    [data-testid="copy-button"], [data-testid="like"], [data-testid="dislike"],
    .message-wrap .icon-button, .message-wrap .icon-buttons,
    .chatbot .icon-button, .chatbot .icon-buttons,
    .chatbot .action-buttons,
    .chatbot button[aria-label="Copy"], .chatbot button[aria-label="Like"],
    .chatbot button[aria-label="Dislike"], .chatbot button[aria-label="Retry"],
    .badge-wrap, .chatbot .badge-wrap,
    span.chatbot-badge, .chatbot-badge,
    .built-with-gradio, a[href*="gradio.app"],
    .show-api, button.show-api, #show-api-btn,
    [class*="show-api"], .api-docs-btn {
        display: none !important;
    }
"""

# Dark theme — emerald/teal accents, dark backgrounds
AGENT_DARK_CSS = """
    html { color-scheme: dark !important; }

    body, .gradio-container, .main, .wrap, .gap, #component-0 {
        background: #0a0f14 !important;
    }

    .block, .form, .panel, .tabitem, .sidebar, .sidebar-content {
        background: #0f1620 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
    }

    input, textarea, .gr-input, .gr-textarea,
    [class*="input-"], [class*="textbox"] {
        background: #162030 !important;
        color: #e2e8f0 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
    }
    input:focus, textarea:focus {
        border-color: #14b8a6 !important;
        box-shadow: 0 0 0 2px rgba(20, 184, 166, 0.25) !important;
    }

    button.primary, .btn-primary, [class*="primary"][class*="btn"] {
        background: #14b8a6 !important;
        border-color: #14b8a6 !important;
        color: #fff !important;
    }
    button.primary:hover, .btn-primary:hover {
        background: #0d9488 !important;
        border-color: #0d9488 !important;
    }

    button.secondary, .btn-secondary {
        background: #162030 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
        color: #5eead4 !important;
    }
    button.secondary:hover { background: #1a3040 !important; }

    .message.bot, .message.assistant,
    [data-testid="bot"], [class*="bot-message"] {
        background: #0f1f2e !important;
        border-left: 3px solid #14b8a6 !important;
        color: #e2e8f0 !important;
    }

    .message.user, [data-testid="user"], [class*="user-message"] {
        background: #1a2a3a !important;
        color: #e2e8f0 !important;
    }

    .markdown, .prose, .gr-markdown, p, span, label, li {
        color: #e2e8f0 !important;
    }

    h1, h2, h3, .markdown h1, .markdown h2, .markdown h3 {
        color: #5eead4 !important;
    }

    .accordion-header, [class*="accordion"] button {
        background: #0f1620 !important;
        color: #5eead4 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
    }

    .dropdown, select {
        background: #162030 !important;
        color: #e2e8f0 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
    }

    code, pre, .gr-code, [class*="code-"] {
        background: #060a10 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
        color: #99f6e4 !important;
    }

    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0a0f14; }
    ::-webkit-scrollbar-thumb {
        background: rgba(20, 184, 166, 0.4);
        border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover { background: #14b8a6; }

    .tab-nav button.selected, [class*="tab"][aria-selected="true"] {
        border-bottom-color: #14b8a6 !important;
        color: #5eead4 !important;
    }

    .sidebar-toggle, [class*="sidebar-toggle"] {
        background: #14b8a6 !important;
        color: #fff !important;
    }
"""

AGENT_PWA_HEAD = """
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="alternate icon" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/icons/icon-192.svg">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#14b8a6">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="protoAgent">
<script>
if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
        navigator.serviceWorker
            .register('/sw.js', { scope: '/' })
            .then(function (reg) {
                console.log('[protoAgent] SW registered:', reg.scope);
            })
            .catch(function (err) {
                console.warn('[protoAgent] SW registration failed:', err);
            });
    });
}
</script>
"""

ChatFn = Callable[[str, str], Awaitable[list[dict]]]
StreamingChatFn = Callable[..., Any]  # generator function
SettingsCallbacks = dict[str, Any]


def create_chat_app(
    chat_fn: ChatFn,
    title: str = "Chat",
    subtitle: str = "",
    placeholder: str = "Type a message...",
    chat_height: str = "80vh",
    footer_html: str = '<div style="text-align:center; padding:8px 0; opacity:0.5; font-size:12px;">built with <a href="https://protolabs.studio" target="_blank" rel="noopener" style="color:inherit;">protolabs.studio</a></div>',
    extra_css: str = "",
    settings: SettingsCallbacks | None = None,
    pwa: bool = True,
    streaming_chat_fn: StreamingChatFn | None = None,
) -> gr.Blocks:
    _theme = gr.themes.Soft(primary_hue="teal", neutral_hue="slate")
    _css = CLEAN_CSS + AGENT_DARK_CSS + extra_css
    _head = AGENT_PWA_HEAD if pwa else ""

    # Determine first-run state. Fresh clones land in the wizard;
    # subsequent boots go straight to chat unless the user explicitly
    # triggers "Re-run setup" from the drawer. Settings dicts without
    # the ``is_setup_complete`` key (older template forks) skip the
    # wizard entirely — chat is always visible.
    setup_done = True
    if settings and "is_setup_complete" in settings:
        try:
            setup_done = bool(settings["is_setup_complete"]())
        except Exception:
            setup_done = True  # fail-open: don't trap forks in a broken wizard
    wizard_enabled = bool(
        settings and "finish_setup" in settings and "get_config" in settings
    )

    def _build() -> gr.Blocks:
        with gr.Blocks(
            title=title.replace("*", "").strip(),
            theme=_theme,
            css=_css,
            head=_head,
            analytics_enabled=False,
        ) as app:
            session_id = gr.State("default")

            header_text = f"**{title}**"
            if subtitle:
                header_text += f" &nbsp; {subtitle}"

            header_md = gr.Markdown(header_text)

            # === SETUP WIZARD PANE =====================================
            # Visible on first run (no .setup-complete marker), hidden
            # after the user clicks Launch. All fields default from the
            # current config so re-running the wizard doesn't start
            # from zero.
            wizard_pane = None
            w_launch_btn = None
            w_launch_status = None
            w_inputs: list = []
            if wizard_enabled:
                with gr.Column(visible=not setup_done) as wizard_pane:
                    gr.Markdown(
                        "# Welcome — let's set up your agent\n\n"
                        "Walk through the steps below and hit **Launch agent**. "
                        "You can revisit every one of these choices later from "
                        "the Configuration drawer. Nothing is persisted until "
                        "you click Launch."
                    )
                    w_launch_status = gr.Markdown("")

                    with gr.Accordion("1. Connect to your model", open=True):
                        w_api_base = gr.Textbox(
                            label="API base URL",
                            placeholder="e.g. https://api.openai.com/v1 or http://localhost:4000/v1",
                            interactive=True,
                        )
                        w_api_key = gr.Textbox(
                            label="API key",
                            type="password",
                            placeholder="your OpenAI or gateway master key",
                            interactive=True,
                        )
                        with gr.Row():
                            w_test_btn = gr.Button(
                                "Test connection & fetch models",
                                variant="secondary", scale=3,
                            )
                        w_test_status = gr.Markdown("")
                        w_model = gr.Dropdown(
                            label="Model",
                            choices=[], allow_custom_value=True,
                            interactive=True,
                        )

                    with gr.Accordion("2. Name your agent", open=False):
                        w_agent_name = gr.Textbox(
                            label="Agent name",
                            placeholder="short lowercase slug, e.g. product-director",
                            interactive=True,
                        )
                        gr.Markdown(
                            "_This becomes the agent card name, OpenAI-compat "
                            "model id, and chat header. Metric prefix still "
                            "needs a process restart to pick up._"
                        )
                        with gr.Row():
                            w_preset = gr.Dropdown(
                                label="Persona preset (optional)",
                                choices=[], interactive=True, scale=3,
                            )
                            w_load_preset_btn = gr.Button(
                                "Load preset into SOUL.md",
                                size="sm", scale=2,
                            )
                        w_soul = gr.Textbox(
                            label="SOUL.md — the agent's persona",
                            lines=14, interactive=True,
                            placeholder=(
                                "Identity, personality, values, communication "
                                "style. Loaded into every system prompt."
                            ),
                        )

                    with gr.Accordion("3. Tools & middleware", open=False):
                        w_tools = gr.CheckboxGroup(
                            label="Tools available to the agent",
                            choices=[], interactive=True,
                        )
                        w_mw_audit = gr.Checkbox(
                            label="Audit middleware — logs every tool call",
                            value=True, interactive=True,
                        )
                        w_mw_memory = gr.Checkbox(
                            label="Memory middleware — persists session summaries",
                            value=True, interactive=True,
                        )
                        w_mw_knowledge = gr.Checkbox(
                            label="Knowledge middleware — requires a knowledge store (leave off for starter setups)",
                            value=False, interactive=True,
                        )

                    with gr.Accordion("4. Optional — you, security, autostart", open=False):
                        w_operator = gr.Textbox(
                            label="Your name",
                            placeholder="so the agent can address you directly — blank = anonymous",
                            interactive=True,
                        )
                        w_auth = gr.Textbox(
                            label="A2A bearer token",
                            type="password",
                            placeholder="set before exposing to a network; blank = open mode for local dev",
                            interactive=True,
                        )
                        w_autostart = gr.Checkbox(
                            label="Launch this agent automatically on login",
                            value=False, interactive=True,
                        )
                        w_autostart_note = gr.Markdown("")

                    w_launch_btn = gr.Button(
                        "Launch agent", variant="primary", size="lg",
                    )

                    w_inputs = [
                        w_api_base, w_api_key, w_model,
                        w_agent_name, w_soul, w_preset,
                        w_tools, w_mw_audit, w_mw_memory, w_mw_knowledge,
                        w_operator, w_auth, w_autostart,
                    ]

            # === CHAT PANE =============================================
            # Wrapped in a Column so visibility toggles in lockstep with
            # the wizard. On fresh setup it starts hidden and the Launch
            # button flips it on.
            with gr.Column(visible=setup_done) as chat_pane:
                chatbot = gr.Chatbot(height=chat_height, show_label=False)

                with gr.Row():
                    txt = gr.Textbox(
                        placeholder=placeholder, show_label=False,
                        scale=9, container=False,
                    )
                    send_btn = gr.Button("Send", variant="primary", scale=1, min_width=80)

                with gr.Row():
                    clear_btn = gr.Button("Clear", size="sm", variant="secondary")
                    new_btn = gr.Button("New Session", size="sm", variant="secondary")

                if footer_html:
                    gr.HTML(footer_html)

            # --- Settings sidebar ---
            # Each section below is gated on the presence of its callback,
            # so forks can opt in per panel. The Configuration panel (the
            # live-editable drawer) renders when "get_config" + "save_all"
            # are provided by the server. The drawer is hidden during the
            # wizard so the user has one surface to look at at a time.
            sidebar_block = None
            if settings:
                with gr.Sidebar(label="Settings", open=False, position="right", visible=setup_done) as sidebar_block:

                    # === Live configuration drawer ============================
                    if "get_config" in settings and "save_all" in settings:
                        gr.Markdown(
                            "### Configuration\n"
                            "Edits are written to `config/langgraph-config.yaml` "
                            "and applied with a live graph rebuild — in-flight "
                            "turns finish on the previous config.",
                        )
                        config_status = gr.Markdown("")

                        with gr.Accordion("Model", open=True):
                            api_base_in = gr.Textbox(
                                label="API Base URL",
                                placeholder="http://gateway:4000/v1",
                                interactive=True,
                            )
                            api_key_in = gr.Textbox(
                                label="API Key",
                                type="password",
                                placeholder="blank → use $OPENAI_API_KEY env",
                                interactive=True,
                            )
                            with gr.Row():
                                model_in = gr.Dropdown(
                                    label="Model",
                                    choices=[],
                                    interactive=True,
                                    allow_custom_value=True,
                                    scale=4,
                                )
                                fetch_models_btn = gr.Button(
                                    "Fetch", size="sm", scale=1, min_width=60,
                                )
                            model_fetch_status = gr.Markdown("")
                            temperature_in = gr.Slider(
                                label="Temperature",
                                minimum=0.0, maximum=2.0, step=0.05,
                                interactive=True,
                            )
                            max_tokens_in = gr.Number(
                                label="Max Tokens", precision=0,
                                minimum=1, interactive=True,
                            )
                            max_iter_in = gr.Slider(
                                label="Max Iterations",
                                minimum=1, maximum=200, step=1,
                                interactive=True,
                            )

                        with gr.Accordion("Worker Subagent", open=False):
                            worker_enabled_in = gr.Checkbox(
                                label="Enabled", interactive=True,
                            )
                            worker_tools_in = gr.CheckboxGroup(
                                label="Tools", choices=[], interactive=True,
                            )
                            worker_max_turns_in = gr.Number(
                                label="Max Turns", precision=0,
                                minimum=1, interactive=True,
                            )

                        with gr.Accordion("Middleware", open=False):
                            mw_knowledge_in = gr.Checkbox(
                                label="Knowledge", interactive=True,
                            )
                            mw_audit_in = gr.Checkbox(
                                label="Audit", interactive=True,
                            )
                            mw_memory_in = gr.Checkbox(
                                label="Memory", interactive=True,
                            )

                        with gr.Accordion("Knowledge Store", open=False):
                            kb_db_in = gr.Textbox(
                                label="DB Path", interactive=True,
                            )
                            kb_embed_in = gr.Textbox(
                                label="Embed Model", interactive=True,
                            )
                            kb_top_k_in = gr.Number(
                                label="Top K", precision=0,
                                minimum=1, interactive=True,
                            )

                        with gr.Accordion("Identity", open=False):
                            identity_name_in = gr.Textbox(
                                label="Agent name",
                                placeholder="short lowercase slug",
                                interactive=True,
                            )
                            identity_operator_in = gr.Textbox(
                                label="Your name (operator)",
                                placeholder="injected into system prompt when set",
                                interactive=True,
                            )

                        with gr.Accordion("Security — A2A bearer token", open=False):
                            auth_token_in = gr.Textbox(
                                label="Bearer token",
                                type="password",
                                placeholder="blank → open mode; set to require Authorization: Bearer <token>",
                                interactive=True,
                            )
                            gr.Markdown(
                                "_Live-reloadable. Save & Reload flips A2A "
                                "enforcement on or off immediately; no restart._"
                            )

                        with gr.Accordion("Autostart on login", open=False):
                            autostart_in = gr.Checkbox(
                                label="Launch this agent automatically on login",
                                interactive=True,
                            )
                            gr.Markdown("")

                        with gr.Accordion("Persona (SOUL.md)", open=False):
                            soul_in = gr.Textbox(
                                label="SOUL.md", lines=16, show_label=False,
                                interactive=True,
                                placeholder="Agent persona — loaded into every system prompt.",
                            )

                        with gr.Row():
                            save_btn = gr.Button(
                                "Save & Reload", variant="primary", scale=2,
                            )
                            reload_btn = gr.Button(
                                "Reload from Disk", variant="secondary", scale=1,
                            )

                        # "Re-run setup" re-opens the wizard with current
                        # values pre-populated — for re-picking a preset,
                        # swapping models, or resetting the autostart plist.
                        if "restart_setup" in settings and wizard_enabled:
                            with gr.Accordion("Re-run setup wizard", open=False):
                                gr.Markdown(
                                    "_Reopens the wizard with all current "
                                    "values pre-filled. Your config isn't "
                                    "wiped — you're just re-visiting the "
                                    "choices._"
                                )
                                reset_setup_btn = gr.Button(
                                    "Run wizard now", variant="secondary",
                                )
                                reset_setup_status = gr.Markdown("")

                        # Ordered tuple used for both load_all outputs and
                        # save_all inputs — keeps the wiring obvious and the
                        # two lists from drifting out of sync.
                        _config_components = [
                            api_base_in, api_key_in, model_in,
                            temperature_in, max_tokens_in, max_iter_in,
                            worker_enabled_in, worker_tools_in, worker_max_turns_in,
                            mw_knowledge_in, mw_audit_in, mw_memory_in,
                            kb_db_in, kb_embed_in, kb_top_k_in,
                            identity_name_in, identity_operator_in,
                            auth_token_in, autostart_in,
                            soul_in,
                        ]

                        def _load_all():
                            cfg = settings["get_config"]()
                            soul = settings["get_soul"]() if "get_soul" in settings else ""
                            tools = settings["list_tools"]() if "list_tools" in settings else []

                            # Best-effort gateway probe. If it fails (offline,
                            # wrong key) we surface the error but keep the form
                            # populated with the saved model name — the user
                            # can still edit everything else.
                            models, err = ([], "")
                            if "list_models" in settings:
                                try:
                                    models, err = settings["list_models"]("", "")
                                except Exception as e:
                                    err = str(e)
                            current_name = cfg["model"]["name"]
                            dropdown_choices = models if models else [current_name]
                            if current_name and current_name not in dropdown_choices:
                                dropdown_choices = [current_name, *dropdown_choices]

                            fetch_msg = (
                                f"✓ {len(models)} model(s) from gateway"
                                if models and not err
                                else f"⚠ {err}" if err else ""
                            )

                            worker = cfg["subagents"]["worker"]
                            identity = cfg.get("identity", {})
                            auth = cfg.get("auth", {})
                            runtime = cfg.get("runtime", {})
                            return (
                                cfg["model"]["api_base"],
                                cfg["model"]["api_key"],
                                gr.update(choices=dropdown_choices, value=current_name),
                                cfg["model"]["temperature"],
                                cfg["model"]["max_tokens"],
                                cfg["model"]["max_iterations"],
                                worker["enabled"],
                                gr.update(choices=tools, value=list(worker["tools"])),
                                worker["max_turns"],
                                cfg["middleware"]["knowledge"],
                                cfg["middleware"]["audit"],
                                cfg["middleware"]["memory"],
                                cfg["knowledge"]["db_path"],
                                cfg["knowledge"]["embed_model"],
                                cfg["knowledge"]["top_k"],
                                identity.get("name", ""),
                                identity.get("operator", ""),
                                auth.get("token", ""),
                                bool(runtime.get("autostart_on_boot", False)),
                                soul,
                                fetch_msg,
                            )

                        def _fetch_models(api_base, api_key):
                            if "list_models" not in settings:
                                return gr.update(), "⚠ list_models not wired"
                            try:
                                models, err = settings["list_models"](api_base, api_key)
                            except Exception as e:
                                return gr.update(), f"⚠ {e}"
                            if err:
                                return gr.update(), f"⚠ {err}"
                            return gr.update(choices=models), f"✓ {len(models)} model(s) from gateway"

                        def _save(
                            api_base, api_key, model_name,
                            temperature, max_tokens, max_iter,
                            worker_enabled, worker_tools, worker_max_turns,
                            mw_knowledge, mw_audit, mw_memory,
                            kb_db, kb_embed, kb_top_k,
                            identity_name, identity_operator,
                            auth_token, autostart_on,
                            soul,
                        ):
                            # Numeric fields fall back to sensible minimums
                            # rather than 0 when the user clears them —
                            # ``validate_config_dict`` rejects zero values so
                            # a blank field would otherwise block the save
                            # with a confusing validation error.
                            new_config = {
                                "model": {
                                    "api_base": api_base or "",
                                    "api_key": api_key or "",
                                    "name": model_name or "",
                                    "temperature": float(temperature),
                                    "max_tokens": int(max_tokens or 4096),
                                    "max_iterations": int(max_iter or 50),
                                },
                                "subagents": {
                                    "worker": {
                                        "enabled": bool(worker_enabled),
                                        "tools": list(worker_tools or []),
                                        "max_turns": int(worker_max_turns or 20),
                                    },
                                },
                                "middleware": {
                                    "knowledge": bool(mw_knowledge),
                                    "audit": bool(mw_audit),
                                    "memory": bool(mw_memory),
                                },
                                "knowledge": {
                                    "db_path": kb_db or "",
                                    "embed_model": kb_embed or "",
                                    "top_k": int(kb_top_k or 1),
                                },
                                "identity": {
                                    "name": (identity_name or "").strip() or "protoagent",
                                    "operator": (identity_operator or "").strip(),
                                },
                                "auth": {
                                    "token": auth_token or "",
                                },
                                "runtime": {
                                    "autostart_on_boot": bool(autostart_on),
                                },
                            }
                            try:
                                ok, msg = settings["save_all"](new_config, soul or "")
                            except Exception as e:
                                return f"⚠ save failed: {e}"
                            return f"{'✓' if ok else '⚠'} {msg}"

                        def _reload_only():
                            try:
                                ok, msg = settings["save_all"](None, None)
                            except Exception as e:
                                return f"⚠ reload failed: {e}"
                            return f"{'✓' if ok else '⚠'} {msg}"

                        app.load(
                            fn=_load_all,
                            outputs=[*_config_components, model_fetch_status],
                        )
                        fetch_models_btn.click(
                            fn=_fetch_models,
                            inputs=[api_base_in, api_key_in],
                            outputs=[model_in, model_fetch_status],
                        )
                        save_btn.click(
                            fn=_save,
                            inputs=_config_components,
                            outputs=[config_status],
                        ).then(
                            fn=_fetch_models,
                            inputs=[api_base_in, api_key_in],
                            outputs=[model_in, model_fetch_status],
                        )
                        reload_btn.click(
                            fn=_reload_only, outputs=[config_status],
                        ).then(
                            fn=_load_all,
                            outputs=[*_config_components, model_fetch_status],
                        )

                    # === Legacy read-only panels (opt-in via their own keys) ==
                    if "get_tools_list" in settings:
                        with gr.Accordion("Tools", open=False):
                            tools_display = gr.Markdown("Loading...")
                            refresh_tools_btn = gr.Button("Refresh", size="sm")

                        def load_tools():
                            return settings["get_tools_list"]()

                        app.load(fn=load_tools, outputs=[tools_display])
                        refresh_tools_btn.click(fn=load_tools, outputs=[tools_display])

                    if "get_model_info" in settings:
                        with gr.Accordion("Model Status", open=False):
                            model_display = gr.Markdown("Loading...")
                            refresh_model_btn = gr.Button("Refresh", size="sm")

                            provider_dropdown = None
                            switch_status = None
                            if "get_provider_choices" in settings:
                                provider_dropdown = gr.Dropdown(
                                    label="Provider", choices=[], interactive=True,
                                )
                                switch_status = gr.Markdown("")

                        def load_model():
                            return settings["get_model_info"]()

                        app.load(fn=load_model, outputs=[model_display])
                        refresh_model_btn.click(fn=load_model, outputs=[model_display])

                        if provider_dropdown is not None:
                            def load_provider_choices():
                                choices = settings["get_provider_choices"]()
                                # get_current_provider is optional — older
                                # forks provided only the choices list.
                                # Missing key must not raise KeyError; the
                                # dropdown simply renders with no preselect.
                                current_fn = settings.get("get_current_provider")
                                current = current_fn() if current_fn else None
                                return gr.update(choices=choices, value=current)

                            def switch_provider(choice):
                                return settings["switch_provider"](choice)

                            def load_subtitle():
                                return settings["get_subtitle"]()

                            app.load(fn=load_provider_choices, outputs=[provider_dropdown])
                            provider_dropdown.change(
                                fn=switch_provider,
                                inputs=[provider_dropdown],
                                outputs=[switch_status],
                            ).then(fn=load_model, outputs=[model_display]).then(
                                fn=load_subtitle, outputs=[header_md],
                            )

                    if "get_knowledge_stats" in settings:
                        with gr.Accordion("Knowledge Base", open=False):
                            kb_display = gr.Markdown("Loading...")
                            refresh_kb_btn = gr.Button("Refresh", size="sm")

                        def load_kb_stats():
                            return settings["get_knowledge_stats"]()

                        app.load(fn=load_kb_stats, outputs=[kb_display])
                        refresh_kb_btn.click(fn=load_kb_stats, outputs=[kb_display])

            # --- Chat callbacks ---

            def add_user_message(message: str, history: list[dict]):
                if not message.strip():
                    return "", history, message
                history.append({"role": "user", "content": message})
                return "", history, message

            def get_response(history: list[dict], original_msg: str, sid: str):
                if not original_msg.strip():
                    return history, sid
                result = asyncio.run(chat_fn(original_msg, sid))
                for msg in result:
                    meta = msg.get("metadata", {})
                    if meta.get("_clear"):
                        return [], sid
                    if meta.get("_new"):
                        return [], secrets.token_hex(4)
                history.extend(result)
                return history, sid

            pending_msg = gr.State("")

            for trigger in [txt.submit, send_btn.click]:
                trigger(
                    fn=add_user_message,
                    inputs=[txt, chatbot],
                    outputs=[txt, chatbot, pending_msg],
                ).then(
                    fn=get_response,
                    inputs=[chatbot, pending_msg, session_id],
                    outputs=[chatbot, session_id],
                )

            clear_btn.click(fn=lambda: ([], "default"), outputs=[chatbot, session_id])
            new_btn.click(fn=lambda: ([], secrets.token_hex(4)), outputs=[chatbot, session_id])

            # --- Wizard callbacks -----------------------------------------
            if wizard_enabled:
                def _load_wizard_defaults():
                    """Seed every wizard field from the current on-disk
                    config. Returns updates in the exact order of
                    ``w_inputs`` plus the connection-test status + the
                    autostart note."""
                    cfg = settings["get_config"]() if "get_config" in settings else {}
                    soul = settings["get_soul"]() if "get_soul" in settings else ""
                    tools = settings["list_tools"]() if "list_tools" in settings else []
                    presets = settings["list_soul_presets"]() if "list_soul_presets" in settings else []

                    model = cfg.get("model", {})
                    identity = cfg.get("identity", {})
                    worker = cfg.get("subagents", {}).get("worker", {})
                    mw = cfg.get("middleware", {})
                    runtime = cfg.get("runtime", {})
                    auth = cfg.get("auth", {})

                    current_model = model.get("name", "")
                    model_choices = [current_model] if current_model else []

                    autostart_msg = ""
                    if "autostart_info" in settings:
                        try:
                            info = settings["autostart_info"]()
                        except Exception as e:
                            info = {"supported": False, "reason": str(e)}
                        if info.get("supported"):
                            state = "installed" if info.get("installed") else "not installed"
                            autostart_msg = f"_Platform supported. Current state: **{state}**._"
                        else:
                            autostart_msg = f"⚠ {info.get('reason', 'not supported on this platform')}"

                    return (
                        model.get("api_base", ""),
                        model.get("api_key", ""),
                        gr.update(choices=model_choices, value=current_model),
                        identity.get("name", ""),
                        soul,
                        gr.update(choices=presets, value=None),
                        gr.update(choices=tools, value=list(worker.get("tools", []))),
                        bool(mw.get("audit", True)),
                        bool(mw.get("memory", True)),
                        bool(mw.get("knowledge", False)),
                        identity.get("operator", ""),
                        auth.get("token", ""),
                        bool(runtime.get("autostart_on_boot", False)),
                        "",  # w_test_status
                        autostart_msg,
                    )

                app.load(
                    fn=_load_wizard_defaults,
                    outputs=[*w_inputs, w_test_status, w_autostart_note],
                )

                # Re-check setup state on every page load so external
                # completions (POST /api/config/setup from curl, or a
                # reset triggered in another tab) are reflected after
                # a browser refresh. Without this, Gradio keeps serving
                # the initial visibility state from when the Blocks
                # were first rendered.
                # Sync visibility on every page load. The output list is
                # either two or three elements depending on whether the
                # sidebar exists — we used to alias the sidebar slot to
                # wizard_pane when missing, but that sent a duplicate
                # gr.update to the same component, which Gradio treats
                # as two competing writes to wizard_pane.visible.
                if sidebar_block is not None:
                    def _sync_visibility_with_sidebar():
                        if "is_setup_complete" not in settings:
                            return gr.update(), gr.update(), gr.update()
                        done = bool(settings["is_setup_complete"]())
                        return (
                            gr.update(visible=not done),  # wizard_pane
                            gr.update(visible=done),      # chat_pane
                            gr.update(visible=done),      # sidebar_block
                        )

                    app.load(
                        fn=_sync_visibility_with_sidebar,
                        outputs=[wizard_pane, chat_pane, sidebar_block],
                    )
                else:
                    def _sync_visibility_no_sidebar():
                        if "is_setup_complete" not in settings:
                            return gr.update(), gr.update()
                        done = bool(settings["is_setup_complete"]())
                        return (
                            gr.update(visible=not done),  # wizard_pane
                            gr.update(visible=done),      # chat_pane
                        )

                    app.load(
                        fn=_sync_visibility_no_sidebar,
                        outputs=[wizard_pane, chat_pane],
                    )

                # Connection test — fills the model dropdown
                def _test_connection(api_base, api_key):
                    if "list_models" not in settings:
                        return gr.update(), "⚠ list_models callback not wired"
                    if not api_base:
                        return gr.update(), "⚠ enter an API base URL first"
                    try:
                        models, err = settings["list_models"](api_base, api_key)
                    except Exception as e:
                        return gr.update(), f"⚠ {e}"
                    if err:
                        return gr.update(), f"⚠ {err}"
                    pick = models[0] if models else None
                    return (
                        gr.update(choices=models, value=pick),
                        f"✓ {len(models)} model(s) — picked **{pick}**, change if needed",
                    )

                w_test_btn.click(
                    fn=_test_connection,
                    inputs=[w_api_base, w_api_key],
                    outputs=[w_model, w_test_status],
                )

                # Preset loader — pastes template text into SOUL textarea
                def _load_preset(name):
                    if not name or "read_soul_preset" not in settings:
                        return gr.update()
                    try:
                        return settings["read_soul_preset"](name)
                    except Exception:
                        return gr.update()

                w_load_preset_btn.click(
                    fn=_load_preset, inputs=[w_preset], outputs=[w_soul],
                )

                # Launch button — write everything, mark complete, then
                # hard-reload the page. Toggling ``visible=`` on nested
                # gr.Column + gr.Sidebar via gr.update is unreliable
                # (children don't always re-mount); a full reload is the
                # only bulletproof way to guarantee the chat pane appears.
                # The reload re-enters _build() which reads
                # is_setup_complete()==True and renders chat + drawer
                # visible from scratch.
                def _finish_wizard(
                    api_base, api_key, model_name,
                    agent_name_val, soul, _preset_unused,
                    tools, mw_audit, mw_memory, mw_knowledge,
                    operator, auth_token, autostart,
                ):
                    if not (api_base or "").strip():
                        return "⚠ API base URL is required — go back to step 1"
                    if not (model_name or "").strip():
                        return "⚠ pick a model — use the Test connection button in step 1"
                    if not (agent_name_val or "").strip():
                        return "⚠ agent name is required — step 2"

                    new_config = {
                        "model": {
                            "api_base": api_base,
                            "api_key": api_key or "",
                            "name": model_name,
                        },
                        "subagents": {
                            "worker": {
                                "enabled": True,
                                "tools": list(tools or []),
                            },
                        },
                        "middleware": {
                            "audit": bool(mw_audit),
                            "memory": bool(mw_memory),
                            "knowledge": bool(mw_knowledge),
                        },
                        "identity": {
                            "name": agent_name_val.strip(),
                            "operator": (operator or "").strip(),
                        },
                        "auth": {"token": auth_token or ""},
                        "runtime": {"autostart_on_boot": bool(autostart)},
                    }
                    try:
                        ok, msg = settings["finish_setup"](new_config, soul or "")
                    except Exception as e:
                        return f"⚠ setup failed: {e}"
                    if ok:
                        return f"✓ {msg} — reloading page…"
                    return f"⚠ {msg}"

                # 1. Run the save. 2. On the client, if the status message
                # starts with "✓", reload after a short beat so the user
                # sees the success line. Any warning (⚠) keeps the wizard
                # visible so they can correct and retry.
                w_launch_btn.click(
                    fn=_finish_wizard,
                    inputs=w_inputs,
                    outputs=[w_launch_status],
                ).then(
                    fn=None,
                    inputs=[w_launch_status],
                    outputs=None,
                    js=(
                        "(status) => {"
                        "  if (typeof status === 'string' && status.startsWith('✓')) {"
                        "    setTimeout(() => window.location.reload(), 1000);"
                        "  }"
                        "  return [];"
                        "}"
                    ),
                )

                # "Re-run setup" in the drawer — same reload-after-flip
                # pattern for the reverse direction.
                if "restart_setup" in settings:
                    def _trigger_rerun():
                        try:
                            msg = settings["restart_setup"]()
                        except Exception as e:
                            return f"⚠ {e}"
                        return f"✓ {msg} — reloading page…"

                    reset_setup_btn.click(
                        fn=_trigger_rerun,
                        outputs=[reset_setup_status],
                    ).then(
                        fn=None,
                        inputs=[reset_setup_status],
                        outputs=None,
                        js=(
                            "(status) => {"
                            "  if (typeof status === 'string' && status.startsWith('✓')) {"
                            "    setTimeout(() => window.location.reload(), 800);"
                            "  }"
                            "  return [];"
                            "}"
                        ),
                    )

        return app

    app = _build()

    _original_launch = app.launch

    def _launch(**kwargs):
        kwargs.setdefault("server_name", "0.0.0.0")
        if kwargs.pop("pwa", None) is not None:
            try:
                return _original_launch(**kwargs, pwa=True)
            except TypeError:
                pass
        return _original_launch(**kwargs)

    app.launch = _launch
    return app
