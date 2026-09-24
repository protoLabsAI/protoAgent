import { PanelHeader } from "@protolabsai/ui/navigation";
import { DropdownSelect, Switch } from "@protolabsai/ui/forms";

import { EDITOR_OPTIONS, isEditorId } from "../lib/editorLinks";
import { setEditorPref, useEditorPref } from "../lib/editorPref";
import { useUI } from "../state/uiStore";

// Settings → Chat: client-side display preferences for the chat transcript. These live in the
// persisted UI store (this device), NOT the agent config — they change what THIS console shows,
// not how the agent behaves. First member: the per-turn token/cost + context-window footer (#1372).
export function ChatSettingsPanel() {
  const showChatUsage = useUI((s) => s.showChatUsage);
  const setShowChatUsage = useUI((s) => s.setShowChatUsage);
  const editor = useEditorPref();

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
        {/* Per-viewer (lib/editorPref.ts), not the per-agent UI store: the editor is a
            property of THIS machine, and it doesn't change when you switch fleet agents. */}
        <div className="setting-row" data-key="chat.openFilesIn">
          <div className="setting-meta">
            <span className="setting-label">Open files in</span>
            <p className="setting-desc">
              File paths in tool results (read, search, find, write, edit) become links that open in
              this editor, at the line when there is one. Works when this console and the agent share
              a filesystem — the desktop app or a local server.
            </p>
          </div>
          <DropdownSelect
            id="chat-open-files-in"
            aria-label="Open files in"
            value={editor}
            onValueChange={(v) => {
              if (isEditorId(v)) setEditorPref(v);
            }}
            options={EDITOR_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          />
        </div>
      </div>
    </section>
  );
}
