// The phone session sheet's pinned "From others" item (docs/design/other-conversations.md §9 C7b).
// createRoot/act + the real chat-store and context-menu registry, like the other console UI suites
// (MobileShell.test.tsx) — the Conversations data layer is mocked so the item renders with
// representative counts without hydrating from the server.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// SessionSheet reads `useConversations` for the pinned item; keep every other export real (chat-store
// imports `setStage` from here) and only swap the hook so the item renders with representative counts
// without hydrating from the server. `vi.hoisted` makes the state reachable from the hoisted factory.
const { convState } = vi.hoisted(() => ({
  convState: { tabVisible: true, count: 2, running: 1, needsYou: 0, stage: "chat" as const },
}));
vi.mock("../chat/conversations-store", async (importActual) => ({
  ...(await importActual<typeof import("../chat/conversations-store")>()),
  useConversations: () => convState,
}));

// The real registry (the barrel registers the core menus on import) resolves the chat-tab menu the
// long-press opens — the same path the desktop tab uses, so this proves the phone's label-less call
// lands on the shared "From others" default (§9 C7a/C7b).
import { chatStore } from "../chat/chat-store";
import { homeQueryKey } from "../chat/homeTab";
import { resolveMenu, useContextMenuStore } from "../contextMenu";
import { queryClient } from "../lib/queryClient";
import { SessionSheet } from "./SessionSheet";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useContextMenuStore.setState({ open: false, type: "", x: 0, y: 0, ctx: undefined });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function mount() {
  act(() => root.render(<SessionSheet open onClose={() => {}} />));
}

// The DS Drawer portals its content under <body>, so query from the document (AppDrawer.test pattern).
const convItem = () => document.querySelector<HTMLButtonElement>(".session-sheet-convo");

describe("SessionSheet pinned 'From others' item (§9 C7b)", () => {
  it("labels the item 'From others' and names it 'From others' for the screen reader", () => {
    mount();
    const item = convItem();
    expect(item, "expected the .session-sheet-convo item").not.toBeNull();
    expect(item!.querySelector(".session-sheet-convo-title")?.textContent).toBe("From others");
    expect(item!.getAttribute("aria-label")).toBe("From others, 2 conversations, 1 running, read-only");
    // No stale "Conversations" wording survives on the item (acceptance r2).
    expect(item!.textContent).not.toMatch(/Conversations/);
    expect(item!.getAttribute("aria-label")).not.toMatch(/Conversations/);
  });

  it("long-press opens the shared chat-tab menu whose Hide entry reads “From others”", () => {
    mount();
    const item = convItem()!;
    vi.useFakeTimers();
    try {
      act(() => item.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 1, isPrimary: true, button: 0, clientX: 10, clientY: 20 })));
      act(() => { vi.advanceTimersByTime(500); });
    } finally {
      vi.useRealTimers();
    }
    const menu = useContextMenuStore.getState();
    expect(menu.type).toBe("chat-tab");
    const ctx = menu.ctx as { onHideConversations?: () => void; hideConversationsLabel?: string };
    expect(typeof ctx.onHideConversations).toBe("function");
    // The phone passes NO explicit label — it relies on the shared default that §9 C7b renamed.
    expect(ctx.hideConversationsLabel).toBeUndefined();
    const entries = resolveMenu("chat-tab", ctx) as { id: string; label?: string }[];
    expect(entries.map((e) => e.id)).toEqual(["hide-conversations"]);
    expect(entries[0].label).toBe("Hide “From others”");
  });
});

describe("SessionSheet pinned Home row (H-U1)", () => {
  const rows = () => Array.from(document.querySelectorAll<HTMLElement>(".session-sheet-row"));
  const title = (row: HTMLElement) => row.querySelector(".session-sheet-title")?.textContent;

  beforeEach(() => {
    queryClient.clear();
    chatStore.ensureSession("sheet-a", "Chat A");
    chatStore.ensureSession("home", "Home"); // the store appends Home; the sheet lists it first
  });

  it("lists Home first with no delete control, and keeps delete on ordinary chats", () => {
    queryClient.setQueryData(homeQueryKey(), { session_id: "home", last_read_at: null });
    mount();
    expect(title(rows()[0])).toBe("Home");
    expect(rows()[0].querySelector(".session-sheet-del")).toBeNull();
    const other = rows().find((row) => title(row) === "Chat A")!;
    expect(other.querySelector(".session-sheet-del")).not.toBeNull();
  });

  it("with no Home state (flag off) the list keeps store order and every row is deletable", () => {
    queryClient.setQueryData(homeQueryKey(), null);
    mount();
    expect(title(rows()[0])).not.toBe("Home");
    expect(rows().every((row) => row.querySelector(".session-sheet-del"))).toBe(true);
  });
});
