import { useSyncExternalStore } from "react";

// The console's resolved light/dark mode, for the code pane's highlighter theme: the explicit
// `data-theme` force on <html> when the theme machinery set one (agentTheme.ts), else the OS
// preference — the same resolution PluginView forwards to plugin iframes.

export type ThemeMode = "light" | "dark";

export function currentThemeMode(): ThemeMode {
  try {
    const forced = document.documentElement.getAttribute("data-theme");
    if (forced === "light" || forced === "dark") return forced;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function subscribe(cb: () => void): () => void {
  // `protoagent:theme` fires on every data-theme/style change (agentTheme.watchThemeChanges).
  window.addEventListener("protoagent:theme", cb);
  let mq: MediaQueryList | null = null;
  try {
    mq = window.matchMedia("(prefers-color-scheme: light)");
    mq.addEventListener("change", cb);
  } catch {
    mq = null;
  }
  return () => {
    window.removeEventListener("protoagent:theme", cb);
    mq?.removeEventListener("change", cb);
  };
}

export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, currentThemeMode, () => "dark");
}
