import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { McpPanel } from "./McpPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const toast = vi.hoisted(() => vi.fn());
vi.mock("@protolabsai/ui/overlays", () => ({
  useToast: () => toast,
  // The REAL QuickSetting (the "Sharing" chip) renders through this, so its dialog copy
  // is part of the panel's visible text under test.
  Dialog: ({ open, title, children, footer }: { open: boolean; title: string; children: ReactNode; footer?: ReactNode }) =>
    open ? h("div", { role: "dialog" }, h("h2", null, title), children, footer) : null,
  ConfirmDialog: ({ open, title, children, onConfirm, confirmLabel }: {
    open: boolean; title: string; children: ReactNode; onConfirm: () => void; confirmLabel: string;
  }) => open ? h("div", { role: "dialog" }, title, children, h("button", { onClick: onConfirm }, confirmLabel)) : null,
}));
vi.mock("@protolabsai/ui/forms", () => ({
  Input: (props: Record<string, unknown>) => h("input", props),
  Textarea: (props: Record<string, unknown>) => h("textarea", props),
  DropdownSelect: ({ options, onValueChange, ...props }: {
    options: { value: string; label: string }[]; onValueChange: (value: string) => void;
  }) => h("select", { ...props, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onValueChange(e.target.value) },
    options.map(({ value, label }) => h("option", { key: value, value }, label))),
}));
vi.mock("./NodeRuntimeCard", () => ({ NodeRuntimeCard: () => null }));
vi.mock("./McpCatalogDialog", () => ({
  McpCatalogDialog: ({ open }: { open: boolean }) => open ? h("div", { role: "dialog" }, "Server catalog") : null,
}));
let container: HTMLDivElement;
let root: Root;
let client: QueryClient;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  toast.mockClear();
});
afterEach(() => {
  act(() => root.unmount());
  client.clear();
  container.remove();
  vi.restoreAllMocks();
});
function mount(servers: { name: string; transport: string; tool_count: number; tier?: "commons" | "private" | "managed" | null }[] = []) {
  client.setQueryData<unknown>(runtimeStatusQuery().queryKey, { mcp: { enabled: true, servers, tool_count: 0 } });
  act(() => root.render(h(QueryClientProvider, { client }, h(McpPanel))));
}
function button(text: string) {
  const found = [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);
  expect(found, text).toBeDefined();
  return found!;
}
function change(selector: string, value: string) {
  const el = container.querySelector(selector) as HTMLInputElement | HTMLSelectElement;
  const proto = el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
}
function expectPlainCopy() {
  expect(container.textContent).not.toMatch(/\b(commons|box|layered|stdio)\b/i);
  for (const el of container.querySelectorAll("[title], [aria-label]")) {
    expect(`${el.getAttribute("title")} ${el.getAttribute("aria-label")}`).not.toMatch(/\b(commons|box|layered|stdio)\b/i);
  }
}
it("explains servers and offers catalog browsing or a custom connection when empty", () => {
  mount();
  expect(container.textContent).toContain("Outside apps that give the agent extra tools · 0 servers");
  expect(container.textContent).toContain("No servers connected yet.");
  expect(button("Browse servers").className).toContain("pl-btn--primary");
  expect(button("Add your own").className).toContain("pl-btn--ghost");
  act(() => button("Browse servers").click());
  expect(container.querySelector('[role="dialog"]')?.textContent).toBe("Server catalog");
  expectPlainCopy();
});
it.each(["stdio", "http", "sse"])("connects %s using plain choices and keeps SSE under Advanced", async (transport) => {
  const add = vi.spyOn(api, "addMcpServer").mockResolvedValue({ ok: true, name: "example", servers: ["example"] });
  vi.spyOn(api, "runtimeStatus").mockRejectedValue(new Error("temporarily unavailable"));
  mount();
  act(() => button("Add your own").click());
  const location = container.querySelector('select[aria-label="Server location"]') as HTMLSelectElement;
  expect([...location.options].map((o) => o.textContent)).toEqual(["Runs on this computer", "Web address"]);
  change('input[placeholder="name (e.g. echo)"]', "example");
  if (transport === "stdio") {
    change('input[placeholder="command (e.g. python)"]', "python");
  } else {
    change('select[aria-label="Server location"]', "http");
    change('input[placeholder="url (https://…)"]', "https://example.test/mcp");
    const advanced = container.querySelector("details")!;
    expect(advanced.open).toBe(false);
    expect(advanced.querySelector("summary")?.textContent).toBe("Advanced");
    act(() => advanced.querySelector("summary")!.click());
    expect(advanced.open).toBe(true);
    const connection = container.querySelector('select[aria-label="Connection type"]') as HTMLSelectElement;
    expect(connection.value).toBe("http");
    expect([...connection.options].map((o) => o.textContent)).toEqual(["Standard", "Older servers (SSE)"]);
    if (transport === "sse") change('select[aria-label="Connection type"]', "sse");
  }
  expectPlainCopy();
  act(() => button("Connect").click());
  await vi.waitFor(() => expect(add).toHaveBeenCalledWith(transport === "stdio"
    ? { name: "example", transport, command: "python", args: "" }
    : { name: "example", transport, url: "https://example.test/mcp" }));
  await vi.waitFor(() => expect(button("Add your own")).toBeDefined());
  act(() => button("Add your own").click());
  expect((container.querySelector('select[aria-label="Server location"]') as HTMLSelectElement).value).toBe("stdio");
});
it("labels shared servers and preserves share, refresh, and confirmed unshare actions", async () => {
  const privateServer = { name: "example", transport: "sse", tool_count: 2, tier: "private" as const };
  const sharedServer = { ...privateServer, tier: "commons" };
  const promote = vi.spyOn(api, "promoteMcpServer").mockResolvedValue({ ok: true, promoted: true, name: "example" });
  const forget = vi.spyOn(api, "forgetMcpServer").mockResolvedValue({ ok: true, forgotten: true, name: "example" });
  const refresh = vi.spyOn(api, "runtimeStatus").mockResolvedValue({ mcp: { servers: [sharedServer] } } as Awaited<ReturnType<typeof api.runtimeStatus>>);
  mount([privateServer]);
  expect(container.textContent).toContain("Outside apps that give the agent extra tools · 1 server");
  expect(container.querySelector(".mcp-server-name")?.textContent).not.toContain("sse");
  expect(button("Share").title).toBe("Shared with every agent on this computer");
  expectPlainCopy();
  act(() => button("Share").click());
  await vi.waitFor(() => expect(promote).toHaveBeenCalledWith("example"));
  await vi.waitFor(() => expect(button("Shared")).toBeDefined());
  expect(container.querySelector(".mcp-server-name")?.textContent).toContain("Shared");
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ message: "Shared example with every agent on this computer." }));
  expectPlainCopy();
  act(() => button("Shared").click());
  expect(container.querySelector('[role="dialog"]')?.textContent).toContain("Stop sharing example?");
  expect(forget).not.toHaveBeenCalled();
  expectPlainCopy();
  refresh.mockResolvedValue({ mcp: { servers: [privateServer] } } as Awaited<ReturnType<typeof api.runtimeStatus>>);
  act(() => button("Unshare").click());
  await vi.waitFor(() => expect(forget).toHaveBeenCalledWith("example"));
  await vi.waitFor(() => expect(button("Share")).toBeDefined());
});
it("opens Sharing with plain choices that still save the stored value", async () => {
  // The field as the server's settings schema emits it (pinned by
  // tests/test_settings_schema.py::test_mcp_sharing_field_reads_in_plain_words).
  const field = {
    key: "mcp.scope", label: "MCP server sharing", type: "select", section: "MCP", restart: false,
    description: "Which MCP servers this agent uses: only the ones added to this agent, or those plus the servers shared with every agent on this computer.",
    options: ["scoped", "layered"],
    option_labels: { scoped: "Only this agent's servers", layered: "This agent's servers and shared ones" },
    value: "scoped", default: "", scope: "agent", source: "default",
  };
  const save = vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [] } as unknown as Awaited<ReturnType<typeof api.saveSettings>>);
  mount();
  client.setQueryData<unknown>(settingsSchemaQuery().queryKey, { groups: [{ section: "MCP", fields: [field] }] });
  act(() => (container.querySelector('button[aria-label="Sharing"]') as HTMLButtonElement).click());
  const dialog = container.querySelector('[role="dialog"]')!;
  expect(dialog.querySelector("h2")?.textContent).toBe("Sharing");
  const select = dialog.querySelector("select") as HTMLSelectElement;
  expect([...select.options].map((o) => o.textContent)).toEqual(["Only this agent's servers", "This agent's servers and shared ones"]);
  expectPlainCopy();
  change('[role="dialog"] select', "layered");
  act(() => button("Save").click());
  await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ "mcp.scope": "layered" }, "agent"));
});
