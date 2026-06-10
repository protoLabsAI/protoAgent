import { QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
// ADR 0037 — design-system foundation. Order matters: brand tokens (--pl-*) first, then
// Tailwind + the shadcn→token bridge, then the legacy theme.css (which may reference --pl-*).
import "@protolabsai/design/css/tokens";
import "./app/tailwind.css";
import "@protolabsai/ui/styles.css";
import "./app/theme-base.css"; // shared token bridge + resets — must load before the rest
import "./app/theme.css";
import { activateSlugAgent } from "./lib/api";
import { queryClient } from "./lib/queryClient";
import { watchThemeChanges } from "./lib/agentTheme";

watchThemeChanges(); // fire `protoagent:theme` on any theme change → plugin iframes repaint live
void activateSlugAgent(); // cold-agent resume + keep-warm touch on slug navigation (#806)

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
