import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ChatSurface } from "./ChatSurface";
import { chatStore } from "./chat-store";
import { openContextMenu } from "../contextMenu";
import { pushRename } from "./sessionListSync";

vi.mock("@tanstack/react-query", async (original) => ({
  ...await original<typeof import("@tanstack/react-query")>(),
  useQuery: () => ({ data: { goals: [] }, refetch: vi.fn() }),
}));
vi.mock("./ChatSessionSlot", async (original) => ({
  ...await original<typeof import("./ChatSessionSlot")>(),
  ChatSessionSlot: () => null,
}));
vi.mock("../contextMenu", () => ({ openContextMenu: vi.fn() }));
vi.mock("./sessionListSync", () => ({ pushRename: vi.fn(), pushClose: vi.fn() }));
vi.mock("@protolabsai/ui/overlays", async (original) => ({
  ...await original<typeof import("@protolabsai/ui/overlays")>(),
  useToast: () => vi.fn(),
}));

describe("ChatSurface inline tab rename", () => {
  let container: HTMLDivElement;
  let root: Root;
  let sessionId: string;
  let originalTitle: string;

  beforeEach(() => {
    vi.clearAllMocks();
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false, addEventListener() {}, removeEventListener() {},
    });
    const session = chatStore.getSnapshot().sessions[0];
    sessionId = session.id;
    originalTitle = session.title;
    chatStore.renameSession(sessionId, "Original chat");
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    chatStore.renameSession(sessionId, originalTitle);
    vi.restoreAllMocks();
  });

  it.each(["double-click", "context-menu Rename"])(
    "%s edits inside the tab and Enter saves the session title",
    async (gesture) => {
      await act(async () => root.render(h(ChatSurface, { onError: vi.fn() })));
      const tab = container.querySelector<HTMLElement>(".pl-tabbar__tab")!;
      expect(tab.textContent).toContain("Original chat");
      await act(async () => {
        tab.dispatchEvent(new MouseEvent(gesture === "double-click" ? "dblclick" : "contextmenu", { bubbles: true }));
      });
      if (gesture === "context-menu Rename") {
        const context = vi.mocked(openContextMenu).mock.calls[0][2] as { onRename: () => void };
        await act(async () => context.onRename());
      }

      const inputs = container.querySelectorAll<HTMLInputElement>('input[aria-label="Rename tab"]');
      expect(inputs).toHaveLength(1);
      const input = inputs[0];
      expect(input.closest(".pl-tabbar__tab")).toBe(tab);
      expect(container.querySelector(".pl-tabbar__editor")).toBeNull();
      expect(input.value).toBe("Original chat");
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Renamed chat");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => {
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      });

      expect(chatStore.getSnapshot().sessions.find((session) => session.id === sessionId)?.title).toBe("Renamed chat");
      expect(pushRename).toHaveBeenCalledWith(sessionId, "Renamed chat");
      expect(tab.textContent).toContain("Renamed chat");
      expect(container.querySelector('input[aria-label="Rename tab"]')).toBeNull();
    },
  );
});
