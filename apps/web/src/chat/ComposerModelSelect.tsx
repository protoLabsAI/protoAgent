import { useQuery } from "@tanstack/react-query";

import { Badge } from "@protolabsai/ui/primitives";
import { Menu, MenuItem } from "@protolabsai/ui/menu";

import { settingsSchemaQuery } from "../lib/queries";
import { chatStore, useChatState } from "./chat-store";

// The composer's inline model picker — rendered in the DS PromptInput `actions` slot.
// This is a PER-TAB override: it does NOT change the saved global model (that lives in
// Settings). The choice is stored on the chat session and sent with each turn, so each
// tab can talk to its own model. Selecting the default-badged model clears the override
// → the configured global model. Available models come from the settings schema's
// `model.name` options (the gateway's live model list), the same source the wizard's
// picker uses.
export function ComposerModelSelect() {
  const schema = useQuery(settingsSchemaQuery());
  const { sessions, currentSessionId } = useChatState();
  const field = schema.data?.groups.flatMap((g) => g.fields).find((f) => f.key === "model.name");

  const globalModel = String(field?.value ?? "");
  const options = field?.options?.length ? field.options : globalModel ? [globalModel] : [];
  const session = sessions.find((s) => s.id === currentSessionId);
  const selected = session?.model ?? "";

  if (!options.length || !currentSessionId) return null;

  const effectiveModel = selected || globalModel;

  return (
    <Menu
      trigger={
        <button type="button" className="composer-model-select" aria-label="Model for this chat">
          {effectiveModel}
        </button>
      }
      align="start"
    >
      {options.map((m) => {
        const isDefault = m === globalModel;
        return (
          <MenuItem
            key={m}
            onSelect={() => {
              chatStore.setSessionModel(
                currentSessionId,
                isDefault ? "" : m,
              );
            }}
          >
            {m}
            {isDefault ? <Badge>default</Badge> : null}
          </MenuItem>
        );
      })}
    </Menu>
  );
}
