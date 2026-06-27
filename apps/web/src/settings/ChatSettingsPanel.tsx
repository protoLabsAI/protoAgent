import { PanelHeader } from "@protolabsai/ui/navigation";
import { Switch } from "@protolabsai/ui/forms";

import { useUI } from "../state/uiStore";

// Settings → Chat: client-side display preferences for the chat transcript. These live in the
// persisted UI store (this device), NOT the agent config — they change what THIS console shows,
// not how the agent behaves. First member: the per-turn token/cost + context-window footer (#1372).
export function ChatSettingsPanel() {
  const showChatUsage = useUI((s) => s.showChatUsage);
  const setShowChatUsage = useUI((s) => s.setShowChatUsage);

  return (
    <section className="panel stage-panel">
      <PanelHeader title="Chat" kicker="how this console renders the transcript — saved on this device" />
      <div className="stage-body">
        <div className="setting-row" data-key="chat.showUsage">
          <div className="setting-meta">
            <span className="setting-label">Token &amp; cost footer</span>
            <p className="setting-desc">
              Show the context-window meter, output tokens, and cost under each answer. Turn off for
              a cleaner transcript.
            </p>
          </div>
          <Switch
            id="chat-show-usage"
            checked={showChatUsage}
            onCheckedChange={setShowChatUsage}
            label={showChatUsage ? "on" : "off"}
          />
        </div>
      </div>
    </section>
  );
}
