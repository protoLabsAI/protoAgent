import { useEffect, useState } from "react";
import { getHostBridge, registerContextMenu } from "@protoagent/plugin-ui";

// Exposed as ./Panel and mounted by the console's FederatedView (ADR 0034 slice 1).
// The useState hook proves this remote shares the HOST's React — a second React copy
// would throw "invalid hook call" the moment this renders.
export default function Panel() {
  const [n, setN] = useState(0);

  // ADR 0034 S2 — the remote consumes the @protoagent/plugin-ui SDK. Because the host shares the
  // SDK as a federation singleton, this registers into the HOST's context-menu registry: the item
  // shows up when you right-click a rail surface, proving cross-boundary plugin menu contribution
  // (ADR 0036). Returns the unregister fn so it's cleaned up on unmount.
  useEffect(() => registerContextMenu({
    type: "rail-surface",
    priority: -10, // after the host's own items
    items: [{ id: "hello-plugin-demo", label: "Hello from the React plugin 👋", run: () => setN((x) => x + 1) }],
  }), []);

  // The host bridge gives the remote authed host context without importing host internals.
  let brand = "the console";
  try { brand = getHostBridge().brandName; } catch { /* bridge unset (standalone build) */ }

  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
      <strong>Hello from a React plugin remote 👋</strong>
      <span style={{ fontSize: "0.85rem", opacity: 0.8 }}>
        A federated Module Federation remote, mounted directly into {brand}'s React tree — not an
        iframe. It shares the host's React + query cache, and uses the @protoagent/plugin-ui SDK
        (ADR 0034). Right-click a rail icon to see the menu item this plugin registered.
      </span>
      <button type="button" onClick={() => setN((x) => x + 1)} style={{ alignSelf: "flex-start" }}>
        clicked {n}× (shared-React hook works)
      </button>
    </div>
  );
}
