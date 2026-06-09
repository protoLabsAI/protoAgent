// First-party optional surface. Workflows is now an opt-in plugin (plugins/workflows),
// so its **Studio** console surface registers through the ext seam and is gated on that
// plugin being enabled (`requiresPlugin`) — instead of being hardcoded in App.tsx. It's
// native React (not an iframe): the trusted, in-process path, just decoupled from core.
import { Boxes } from "lucide-react";

import { WorkflowsSurface } from "../workflows/WorkflowsSurface";
import { registerSurface } from "./registry";

registerSurface({
  id: "studio",
  label: "Studio",
  icon: <Boxes size={18} />,
  requiresPlugin: "workflows",
  render: () => <WorkflowsSurface />,
});
