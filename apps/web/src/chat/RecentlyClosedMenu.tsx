import { History } from "lucide-react";

import { Menu, MenuItem } from "@protolabsai/ui/menu";

import { chatStore, useClosedSessions } from "./chat-store";

// "Recently closed" menu beside the chat tabs (#1525): lists soft-closed tabs newest-first and
// reopens the picked one (reconnecting its server checkpoint with full agent context). Hidden
// when nothing is stashed, so the tab row is unchanged until you've closed a tab. The keyboard
// reopen is Cmd/Ctrl+Shift+T (chat.reopen); this is the discoverable, click-to-pick counterpart.
export function RecentlyClosedMenu() {
  const closed = useClosedSessions();
  if (closed.length === 0) return null;
  return (
    <Menu
      trigger={
        <button
          type="button"
          className="chat-recently-closed"
          aria-label="Recently closed chats"
          title="Recently closed chats"
        >
          <History size={15} />
        </button>
      }
    >
      {closed.map((s) => (
        <MenuItem key={s.id} onSelect={() => chatStore.reopenSession(s.id)}>
          {s.title || "New chat"}
        </MenuItem>
      ))}
    </Menu>
  );
}
