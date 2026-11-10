import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { AppDrawer } from "./AppDrawer";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let container: HTMLDivElement;
const onClose = vi.fn();
const onSelectSurface = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function mount(badges?: Record<string, number>, mobile = true) {
  act(() => root.render(
    <AppDrawer
      open
      onClose={onClose}
      mobile={mobile}
      surfaces={[
        { id: "work", label: "Work", icon: null },
        { id: "chat", label: "Chat", icon: null },
      ]}
      badges={badges}
      activeSurface="chat"
      onSelectSurface={onSelectSurface}
      onOpenGlobal={vi.fn()}
    />,
  ));
}

const rows = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".app-drawer-group:first-child button"));

it.each<Record<string, number> | undefined>([undefined, { work: 0 }, { unknown: 2 }])("keeps rows unchanged for badges %j", (badges) => {
  mount(badges);
  expect(rows().map((row) => row.textContent)).toEqual(["Work", "Chat"]);
  for (const row of rows()) {
    expect(row.hasAttribute("aria-label")).toBe(false);
    expect(row.querySelector(".pl-count")).toBeNull();
  }
});

it("shows and announces two Work items needing you, preserving navigation", () => {
  mount({ work: 2 });
  const [work, chat] = rows();
  expect(work.getAttribute("aria-label")).toBe("Work, 2 need you");
  expect(work.querySelector(".pl-count")?.textContent).toBe("2");
  expect(chat.textContent).toBe("Chat");
  expect(chat.querySelector(".pl-count")).toBeNull();
  act(() => work.click());
  expect(onSelectSurface).toHaveBeenCalledWith("work");
  expect(onClose).toHaveBeenCalledOnce();
  mount({ work: 0 });
  expect(rows()[0].textContent).toBe("Work");
  expect(rows()[0].hasAttribute("aria-label")).toBe(false);
});

it("supports counts explicitly supplied for other surfaces", () => {
  mount({ chat: 2 });
  const [work, chat] = rows();
  expect(work.textContent).toBe("Work");
  expect(chat.getAttribute("aria-label")).toBe("Chat, 2 need you");
  expect(chat.querySelector(".pl-count")?.textContent).toBe("2");
});

it("keeps surface navigation out of the desktop drawer", () => {
  mount({ work: 2 }, false);
  expect(document.querySelector("[data-testid='app-drawer']")?.textContent).not.toContain("Go to");
  expect(document.querySelector(".pl-count")).toBeNull();
});
