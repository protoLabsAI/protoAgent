// Settings ▸ Identity copy (designSystem Settings phase 1): the page names the agent and edits
// its persona in plain words — no file name, no internal mechanism. The panel is mounted for
// real (history button included); only the API, the toast/dialog layer and the lazy Markdown
// renderer are stubbed.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  config: vi.fn(),
  soulHistory: vi.fn(),
  saveSettings: vi.fn(),
  applyConfig: vi.fn(),
}));

vi.mock("@protolabsai/ui/overlays", () => ({
  useToast: () => () => "t1",
  Dialog: () => null,
}));
vi.mock("../chat/LazyMarkdown", () => ({
  Markdown: ({ children }: { children: string }) => h("div", { "data-testid": "md" }, children),
}));
vi.mock("../lib/api", () => ({ api: mocks }));

import { IdentityPanel } from "./IdentityPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const JARGON = ["SOUL.md", "cascade", "hot-reload", "A2A"];

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function flush(times = 6) {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

async function mount(soul: string) {
  mocks.config.mockResolvedValue({ config: { identity: { name: "ava" } }, soul });
  mocks.soulHistory.mockResolvedValue({ versions: [] });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root!.render(h(QueryClientProvider, { client }, h(IdentityPanel)));
  });
  await flush();
  return host;
}

/** Everything a person can read on the page: rendered text plus placeholder/title/aria-label. */
function visibleText(el: HTMLElement): string {
  const attrs = Array.from(el.querySelectorAll("*")).flatMap((n) =>
    ["placeholder", "title", "aria-label"].map((a) => n.getAttribute(a) ?? ""),
  );
  return [el.textContent ?? "", ...attrs].join("\n");
}

function expectNoJargon(el: HTMLElement) {
  const text = visibleText(el).toLowerCase();
  for (const word of JARGON) expect(text).not.toContain(word.toLowerCase());
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("IdentityPanel copy", () => {
  it("shows the plain subtitle, the Persona label, a Save button and the empty-preview text", async () => {
    const el = await mount("");

    expect(el.textContent).toContain("Name and persona");
    const label = el.querySelector(".soul-head > span");
    expect(label?.textContent).toBe("Persona");
    const save = el.querySelector('[data-testid="identity-save"]');
    expect(save?.textContent?.trim()).toBe("Save");
    const preview = el.querySelector('[data-testid="identity-soul-preview"]');
    expect(preview?.textContent).toBe("No persona yet. Select **Edit** to write one.");
    expect(el.querySelector(".soul-hint")).toBeNull();
  });

  it("shows no file name or internal term, in preview or in edit mode", async () => {
    const el = await mount("");
    expectNoJargon(el);

    const toggle = el.querySelector<HTMLButtonElement>('[data-testid="identity-soul-toggle"]');
    await act(async () => {
      toggle!.click();
    });
    expect(el.querySelector('[data-testid="identity-soul"]')).not.toBeNull();
    expectNoJargon(el);
  });

  it("renders a written persona instead of the empty-preview text", async () => {
    const el = await mount("# You are Ava");

    const preview = el.querySelector('[data-testid="identity-soul-preview"]');
    expect(preview?.textContent).toBe("# You are Ava");
    expectNoJargon(el);
  });
});
