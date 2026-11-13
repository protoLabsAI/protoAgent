// Whether the agent this setup creates will be able to run code (`execute_code`), so the finish
// step only tells the operator it can when it actually can (Josh's condition for #5168).
//
// The rule is the plugin rule: `execute_code` is on when Cowork is on (Cowork `enables:` it) and
// nothing disables it, or when it is switched on by name. The host's current plugins say where
// things stand now; the picked archetype's bundle can switch plugins on (`enabled`) or off
// (`config.plugins.disabled`, reported as `disabled`). While either is still loading, the answer
// is "yes": saying it can run code when it can't is the safe way for a disclosure to be wrong.

type HostPlugin = { id: string; enabled: boolean };

export function agentWillRunCode(opts: {
  hostPlugins?: HostPlugin[] | null;
  archetypeEnabled?: string[] | null;
  archetypeDisabled?: string[] | null;
}): boolean {
  const host = opts.hostPlugins;
  const on = new Set(opts.archetypeEnabled ?? []);
  const off = new Set(opts.archetypeDisabled ?? []);
  if (off.has("execute_code")) return false;
  if (on.has("execute_code")) return true;
  if (!host) return true; // not loaded yet
  const hostOn = (id: string) => host.find((p) => p.id === id)?.enabled;
  const cowork = !off.has("cowork") && (on.has("cowork") || hostOn("cowork") === true);
  if (!cowork) return false;
  // Cowork is on: execute_code follows unless the host has it off anyway. Only a host where
  // Cowork is ALREADY on can say that (a host-level `plugins.disabled: [execute_code]` shows as
  // Cowork on, execute_code off); with Cowork off there, execute_code is off only for that reason.
  return !(hostOn("cowork") === true && hostOn("execute_code") === false);
}
