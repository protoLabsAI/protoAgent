// The phone header reads "From others" over the read-only Conversations layer, on the list and
// inside a thread (docs/design/other-conversations.md §9 C7b). createRoot/act like the sibling
// MobileShell.test.tsx; the Conversations layer shows only while the store's stage is
// `conversations` and the tab is visible, so we drive that through a mocked hook (the other store
// exports stay real — MobileShell also calls backToList / openChat) and stub the pane so the
// header renders without any thread data.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { conv } = vi.hoisted(() => ({
  conv: {
    tabVisible: false,
    stage: "chat" as "chat" | "conversations",
    openThreadId: null as string | null,
    count: 0,
    running: 0,
    needsYou: 0,
  },
}));
vi.mock("../chat/conversations-store", async (importActual) => ({
  ...(await importActual<typeof import("../chat/conversations-store")>()),
  useConversations: () => conv,
}));
vi.mock("../chat/ConversationsPane", () => ({ ConversationsPane: () => null }));

import { MobileShell } from "./MobileShell";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

const baseProps = {
  root: <div data-testid="chat-root" />,
  pushed: null,
  title: "A pushed surface",
  showBack: false,
  onBack: () => {},
  onOpenDrawer: () => {},
  needsYouCount: 0,
  onOpenWork: () => {},
  sessionSheetOpen: false,
  onSessionSheetChange: () => {},
};

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  Object.assign(conv, { tabVisible: false, stage: "chat", openThreadId: null });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function mount() {
  act(() =>
    root.render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MobileShell {...baseProps} />
      </QueryClientProvider>,
    ),
  );
}

const titleText = () => container.querySelector(".mshell-title-text")?.textContent;

describe("MobileShell 'From others' header (§9 C7b)", () => {
  it("names the switcher 'From others' on the Conversations list", () => {
    Object.assign(conv, { tabVisible: true, stage: "conversations", openThreadId: null });
    mount();
    expect(titleText()).toBe("From others");
    // Nothing user-visible in the phone header still says "Conversations" (acceptance r2).
    expect(container.querySelector(".mshell-head")?.textContent).not.toMatch(/Conversations/);
  });

  it("keeps the centre header 'From others' inside a thread, beside a Back to list control", () => {
    Object.assign(conv, { tabVisible: true, stage: "conversations", openThreadId: "ctx-1" });
    mount();
    expect(titleText()).toBe("From others");
    expect(container.querySelector('[aria-label="Back to list"]')).not.toBeNull();
  });

  it("shows the live chat title (not 'From others') on the chat root", () => {
    mount(); // conv stays stage: chat → no Conversations layer
    expect(titleText()).not.toBe("From others");
  });
});
