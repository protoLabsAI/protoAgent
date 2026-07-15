import { useQuery } from "@tanstack/react-query";

import { Badge } from "@protolabsai/ui/primitives";
import { Menu, MenuItem } from "@protolabsai/ui/menu";

import { acpAgentsQuery, runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { chatStore, useChatState } from "./chat-store";

// The composer's inline model picker — rendered in the DS PromptInput `actions` slot.
// PER-TAB override (does NOT change the saved global model in Settings): the choice is stored
// on the chat session and sent with each turn, so each tab can run on its own model — or, per
// ADR 0082, its own ACP coding agent. The value is a gateway alias, an `acp:<id>` (hot-swap the
// runtime for this chat), or "" (→ the configured global runtime/model).
//
// Options: gateway models from the settings schema's `model.name` (the gateway's live list) PLUS
// the ACP agent catalog (`/api/acp-agents`). Under a GLOBAL ACP runtime a gateway pick is inert
// (the turn still routes to the coding agent — resolve_turn_runtime only overrides for `acp:*`),
// so we offer ACP agents only there and don't show misleading gateway rows.
export function ComposerModelSelect({ onRuntimeSwitch }: { onRuntimeSwitch?: (note: string) => void } = {}) {
  const schema = useQuery(settingsSchemaQuery());
  const runtime = useQuery(runtimeStatusQuery());
  const acp = useQuery(acpAgentsQuery());
  const { sessions, currentSessionId } = useChatState();
  const field = schema.data?.groups.flatMap((g) => g.fields).find((f) => f.key === "model.name");

  const globalModel = String(field?.value ?? "");
  const gatewayModels = field?.options?.length ? field.options : globalModel ? [globalModel] : [];
  const session = sessions.find((s) => s.id === currentSessionId);
  const selected = session?.model ?? ""; // per-tab: "", a gateway alias, or "acp:<id>"

  // Which coding agent (if any) the GLOBAL runtime is, from runtime status.
  const globalRuntime = runtime.data?.agent_runtime ?? "";
  const globalAcpAgent = globalRuntime.startsWith("acp:") ? globalRuntime.slice("acp:".length) : "";

  // ACP agents to offer. Fall back to just the global agent while the catalog is still loading
  // (so a global-ACP instance never renders an empty menu).
  const acpAgents: { id: string; label: string }[] = acp.data?.agents?.length
    ? acp.data.agents
    : globalAcpAgent
      ? [{ id: globalAcpAgent, label: globalAcpAgent }]
      : [];
  const agentLabel = (id: string) => acpAgents.find((a) => a.id === id)?.label ?? id;

  if (!currentSessionId) return null;
  if (!gatewayModels.length && !acpAgents.length) return null;

  // What THIS turn will run on: the per-tab selection wins, else the global runtime.
  const perTabAcpAgent = selected.startsWith("acp:") ? selected.slice("acp:".length) : "";
  const effectiveAcpAgent = perTabAcpAgent || (selected ? "" : globalAcpAgent);
  const effectiveGatewayModel = selected && !perTabAcpAgent ? selected : globalModel;

  // Runtime identity for the Option-C boundary note (ADR 0082 D4): a note fires only when the
  // RUNTIME crosses a boundary (native↔acp, or acp:A↔acp:B) — swapping between two gateway
  // models is continuous (checkpointer keeps history), so it must NOT note.
  const effectiveKey = effectiveAcpAgent ? `acp:${effectiveAcpAgent}` : "native";

  function choose(nextModel: string, nextKey: string, nextLabel: string) {
    if (nextKey !== effectiveKey) {
      onRuntimeSwitch?.(
        `Switched this chat to ${nextLabel}. Earlier context isn't carried across runtimes — this starts a fresh session on the new one.`,
      );
    }
    chatStore.setSessionModel(currentSessionId!, nextModel);
  }

  const trigger = (
    <button type="button" className="composer-model-select" aria-label="Model for this chat">
      {effectiveAcpAgent ? (
        <>
          {agentLabel(effectiveAcpAgent)}
          <Badge>coding agent</Badge>
        </>
      ) : (
        effectiveGatewayModel
      )}
    </button>
  );

  return (
    <Menu trigger={trigger} align="start">
      {/* Gateway models — only under a native global runtime (see header note). Picking the
          global default clears the per-tab override (→ the configured runtime). */}
      {!globalAcpAgent
        ? gatewayModels.map((m) => {
            const isDefault = m === globalModel;
            return (
              <MenuItem key={`gw:${m}`} onSelect={() => choose(isDefault ? "" : m, "native", m)}>
                {m}
                {isDefault ? <Badge>default</Badge> : null}
              </MenuItem>
            );
          })
        : null}
      {/* ACP coding agents — hot-swap the runtime for THIS chat (ADR 0082). Under a global ACP
          runtime, its agent is the "default" (picking it clears the per-tab override). */}
      {acpAgents.map((a) => {
        const isGlobalDefault = !!globalAcpAgent && a.id === globalAcpAgent;
        const isCurrent = a.id === effectiveAcpAgent;
        return (
          <MenuItem key={`acp:${a.id}`} onSelect={() => choose(isGlobalDefault ? "" : `acp:${a.id}`, `acp:${a.id}`, a.label)}>
            {a.label}
            <Badge>coding agent</Badge>
            {isGlobalDefault ? <Badge>default</Badge> : isCurrent ? <Badge>current</Badge> : null}
          </MenuItem>
        );
      })}
    </Menu>
  );
}
