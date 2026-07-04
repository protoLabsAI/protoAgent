import { QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
import { Launcher } from "./app/Launcher";
import { AppCrash } from "./app/AppCrash";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { isLauncherWindow } from "./lib/desktop";
// ADR 0037 — design-system foundation. Order matters: brand tokens (--pl-*) first, then
// Tailwind + the shadcn→token bridge, then the legacy theme.css (which may reference --pl-*).
import "@protolabsai/design/css/tokens";
import "./app/tailwind.css";
import "@protolabsai/ui/styles.css"; // component styles, incl. the DS `.pl-markdown` renderer
import "streamdown/styles.css"; // streaming per-token fade (opt-in; see DS <Markdown> docstring)
import "katex/dist/katex.min.css"; // KaTeX glyph layout for math in the DS <Markdown>
import "./app/theme-base.css"; // shared token bridge + resets — must load before the rest
import "./app/theme.css";
import { activateSlugAgent } from "./lib/api";
import { queryClient } from "./lib/queryClient";
import { watchThemeChanges } from "./lib/agentTheme";

watchThemeChanges(); // fire `protoagent:theme` on any theme change → plugin iframes repaint live

// The desktop quick-launcher window (ADR 0057) boots the same bundle but renders ONLY the
// command palette — no shell, no slug activation. Everything else is the full console.
// The `is-launcher` class scopes launcher.css (its `.pl-cmdk-*` overrides are global in the
// bundle, so without this they'd leak onto the in-app ⌘K palette in the main window).
const launcher = isLauncherWindow();
if (launcher) document.documentElement.classList.add("is-launcher");
if (!launcher) void activateSlugAgent(); // cold-agent resume + keep-warm touch on slug navigation (#806)

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* The ROOT boundary (#872): anything a panel boundary doesn't catch lands on a
        full-page recovery card instead of a white screen. Outermost so a throw in
        the providers themselves is caught too. */}
    <ErrorBoundary fallback={({ error }) => <AppCrash error={error} />}>
      <QueryClientProvider client={queryClient}>
        {/* Toasts anchor TOP-right (app-level notifications; clear of the bottom utility
            bar / composer) via the DS prop — no `.pl-toast-stack` CSS override needed
            since @protolabsai/ui 0.49 (ToastProvider `position`). */}
        <ToastProvider position="top-right">{launcher ? <Launcher /> : <App />}</ToastProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
