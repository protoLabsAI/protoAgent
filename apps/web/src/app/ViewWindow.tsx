// A popped-out plugin view (spike): one surface, full window — no rails, no chat, no
// SSE-driven shell. Boots like the launcher (one runtime-status fetch) and mounts the SAME
// PluginView host the docks use, so the iframe's auth/theme handshake, keybindings and
// in-frame context menu are unchanged.
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { ContextMenuRenderer } from "../contextMenu";
import { is401 } from "../lib/api";
import { brandName } from "../lib/brand";
import { isNavigablePluginView } from "../lib/pluginViews";
import { runtimeStatusQuery } from "../lib/queries";
import { AuthGate } from "./AuthGate";
import { PluginView } from "./PluginView";
import "./view-window.css";

// Same detection App uses: the macOS desktop build hides the title bar, so the native
// traffic lights float over the content and the header must inset + drag.
const isTauriMac =
  typeof window !== "undefined" &&
  (window.location.protocol === "tauri:" ||
    window.location.hostname === "tauri.localhost" ||
    Boolean((window as unknown as { __PROTOAGENT_API_BASE__?: string }).__PROTOAGENT_API_BASE__)) &&
  /Mac/i.test(navigator.userAgent);

export function ViewWindow({ viewKey }: { viewKey: string }) {
  const runtimeQ = useQuery({
    ...runtimeStatusQuery(),
    retry: (failureCount, error) => !is401(error) && failureCount < 30,
    retryDelay: 1000,
  });
  const runtime = runtimeQ.data ?? null;
  // Mirror App's derivation, so a key that resolves in the rail resolves here identically.
  const view =
    (runtime?.plugins ?? [])
      .filter((p) => p.enabled && p.views?.length)
      .flatMap((p) =>
        (p.views ?? []).map((v) => ({
          ...v,
          key: `plugin:${p.id}:${v.id}`,
          pluginLoaded: p.loaded,
          pluginError: p.error,
        })),
      )
      .filter(isNavigablePluginView)
      .find((v) => v.key === viewKey) ?? null;

  const title = view?.label ?? "View";
  useEffect(() => {
    document.title = `${title} · ${brandName(runtime?.identity?.name)}`;
  }, [title, runtime]);

  return (
    <div className={`view-window${isTauriMac ? " is-tauri-mac" : ""}`}>
      <header className="view-window-bar" data-tauri-drag-region>
        <span className="view-window-title" data-tauri-drag-region>
          {title}
        </span>
      </header>
      <main className="view-window-body">
        {view ? (
          <PluginView key={view.key} view={view} />
        ) : (
          <div className="view-window-empty">
            {runtime ? "This view isn't available — its plugin may be disabled or uninstalled." : "Loading…"}
          </div>
        )}
      </main>
      <AuthGate />
      <ContextMenuRenderer />
    </div>
  );
}
