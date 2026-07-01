// ADR 0057 — the Raycast-style desktop quick launcher. A SEPARATE, frameless, always-
// on-top Tauri window (label "launcher") whose webview boots straight into this
// component instead of the full console. It hosts ONLY the command palette, summoned by
// the ⌥Space global shortcut from anywhere and dismissed on blur / Escape.
//
// It reuses the EXACT registry the in-app ⌘K palette uses (usePaletteRegistry), so the
// command list stays in lock-step. The difference is navigation: this window has no
// shell, so its nav commands forward a serializable NavIntent to the main console
// window (setPaletteNavigator) and then hide the launcher; quick-chat and inline plugin
// views run right here in the launcher.
import { useEffect, useMemo, useState } from "react";
import { MessageSquare, Puzzle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CommandPalette } from "@protolabsai/ui/command-palette";
import type { PaletteView } from "@protolabsai/ui/command-palette";
import { setPaletteNavigator, usePaletteRegistry } from "./usePaletteRegistry";
import type { InlinePluginView } from "./usePaletteRegistry";
import { PaletteChat } from "./PaletteChat";
import { CORE_SURFACES } from "./coreSurfaces";
import { buildViews } from "../lib/viewRegistry";
import { runtimeStatusQuery } from "../lib/queries";
import { apiUrl, authToken } from "../lib/api";
import { consoleTheme } from "./PluginView";
import { brandName } from "../lib/brand";
import { emit, invoke, listen } from "../lib/desktop";
import "./launcher.css";

// The launcher keeps its glyphs lightweight: core surfaces carry their own icons; plugin
// views fall back to the generic plugin mark (the rail/⌘K palette resolve the full lucide
// set — not worth pulling that chunk into the launcher window for a secondary surface).
function pluginIcon(): ReactNode {
  return <Puzzle size={18} />;
}

export function Launcher() {
  // Lightweight: one status fetch for the plugin-view + agent-name inputs (no SSE, no
  // shell). The palette's quick-chat + inline plugin views talk to the same sidecar.
  const runtimeQ = useQuery({ ...runtimeStatusQuery() });
  const runtime = runtimeQ.data ?? null;

  // Mirror App's plugin-view derivation: each enabled plugin's declared views become a
  // `plugin:<id>:<view>` surface (chat-slot + utility widgets excluded — same as the rail).
  const allPluginViews = (runtime?.plugins ?? [])
    .filter((p) => p.enabled && p.views?.length)
    .flatMap((p) => (p.views ?? []).map((v) => ({ ...v, key: `plugin:${p.id}:${v.id}` })))
    .filter((v) => v.slot !== "chat" && !v.utility);

  // The same three command sources the in-app palette feeds usePaletteRegistry.
  const { views: paletteViews } = buildViews({
    core: CORE_SURFACES,
    plugins: allPluginViews.map((v) => ({ key: v.key, label: v.label, icon: pluginIcon() })),
    ext: [], // fork/ext surfaces are build-time host concerns — not surfaced from the launcher
  });

  // Plugin views that opted into inline palette morphing render their iframe IN the
  // launcher (themed/authed via the same handshake the in-app palette uses).
  const inlinePaletteViews: InlinePluginView[] = allPluginViews
    .filter((v) => v.palette === "inline" || (typeof v.palette === "object" && v.palette !== null))
    .map((v) => ({
      id: v.key,
      title: v.label,
      url: apiUrl(typeof v.palette === "object" && v.palette?.path ? v.palette.path : v.path),
      icon: pluginIcon(),
      theme: consoleTheme(),
      token: authToken(),
      sandbox: "allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-pointer-lock",
    }));

  // Inline quick-chat with the focused agent (host, since the launcher loads /app/).
  const chatAgentName = brandName(runtime?.identity?.name);
  const paletteChat = useMemo(
    () => ({
      name: chatAgentName,
      icon: <MessageSquare size={16} />,
      view: {
        id: "chat",
        title: chatAgentName,
        width: 620,
        render: () => <PaletteChat agentName={chatAgentName} />,
      } as PaletteView,
    }),
    [chatAgentName],
  );

  const registry = usePaletteRegistry(paletteViews, inlinePaletteViews, paletteChat);

  // Swap the palette's navigation sink: forward the intent to the main console window,
  // bring it to the front, and dismiss the launcher. Restore the default on unmount.
  useEffect(() => {
    setPaletteNavigator((intent) => {
      void emit("palette:navigate", intent);
      void invoke("focus_main");
      void invoke("hide_launcher");
    });
    return () => setPaletteNavigator(null);
  }, []);

  // Open by default; re-summoning (the Rust shell emits "launcher:shown" on show) resets
  // the palette to its root view + refocuses the search by toggling open off→on.
  const [open, setOpen] = useState(true);
  useEffect(() => {
    let raf = 0;
    let off = () => {};
    void listen("launcher:shown", () => {
      setOpen(false);
      raf = requestAnimationFrame(() => setOpen(true));
    }).then((fn) => {
      off = fn;
    });
    return () => {
      off();
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <div className="launcher-root">
      <CommandPalette
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) void invoke("hide_launcher"); // Escape / click-away → hide the window
        }}
        registry={registry}
        // `overlay` floats the palette as a rounded card (vs `fullscreen`'s edge-to-edge
        // fill); launcher.css makes the surrounding scrim transparent so the window's
        // see-through margins + the frosted card read as a Raycast-style panel.
        presentation="overlay"
      />
    </div>
  );
}
