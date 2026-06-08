import { useState } from "react";

// Exposed as ./Panel and mounted by the console's FederatedView (ADR 0034 slice 1).
// The useState hook proves this remote shares the HOST's React — a second React copy
// would throw "invalid hook call" the moment this renders.
export default function Panel() {
  const [n, setN] = useState(0);
  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
      <strong>Hello from a React plugin remote 👋</strong>
      <span style={{ fontSize: "0.85rem", opacity: 0.8 }}>
        A federated Module Federation remote, mounted directly into the console's React tree —
        not an iframe. It shares the host's React + query cache (ADR 0034).
      </span>
      <button type="button" onClick={() => setN((x) => x + 1)} style={{ alignSelf: "flex-start" }}>
        clicked {n}× (shared-React hook works)
      </button>
    </div>
  );
}
