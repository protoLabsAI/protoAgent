import { QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
// ADR 0037 — design-system foundation. Order matters: brand tokens (--pl-*) first, then
// Tailwind + the shadcn→token bridge, then the legacy theme.css (which may reference --pl-*).
import "@protolabsai/design/css/tokens";
import "./app/tailwind.css";
import "./app/theme.css";
import { queryClient } from "./lib/queryClient";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
