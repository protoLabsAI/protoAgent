import { QueryClientProvider } from "@tanstack/react-query";
import { setHostBridge } from "@protoagent/plugin-ui";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
import { api, apiUrl, authToken } from "./lib/api";
import { brandName } from "./lib/brand";
// ADR 0037 — design-system foundation. Order matters: brand tokens (--pl-*) first, then
// Tailwind + the shadcn→token bridge, then the legacy theme.css (which may reference --pl-*).
import "@protolabsai/design/css/tokens";
import "./app/tailwind.css";
import "@protolabsai/ui/styles.css";
import "./app/theme.css";
import { queryClient } from "./lib/queryClient";

// Inject the host bridge once at startup (ADR 0034 D4) — `ui: react` plugin remotes read it via
// getHostBridge() for authed API access + host context, without importing host internals.
setHostBridge({ api, authToken, apiUrl, brandName: brandName() });

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
