// useSlashAutocomplete (#3850) — the composer's `/` command + `@` mention popover,
// extracted from ChatSessionSlot. A minimal createRoot/act renderHook (no testing-library
// dep in the console) over a real textarea + a QueryClient whose server lists come from
// spied `api` calls. Pins: token parsing off the live caret (incl. the native caret
// listeners), client-first + dedup'd `/` matches, the flag gate, `@` ranking by who has
// spoken, ↑/↓/Enter/Tab/Escape selection, and both completion paths.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { registerSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { useSlashAutocomplete, type UseSlashAutocompleteOptions } from "./useSlashAutocomplete";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type HookResult = ReturnType<typeof useSlashAutocomplete>;
type Props = Omit<UseSlashAutocompleteOptions, "textareaRef">;

const clientRun = vi.fn(() => true);

beforeAll(() => {
  // Module-level registry, first-wins: unique names keep this suite independent.
  registerSlashCommand({ name: "zzlocal", description: "a client command", run: clientRun });
  registerSlashCommand({ name: "zzshared", description: "client twin of a server skill", run: () => true });
  registerSlashCommand({ name: "zzflagged", description: "flag-gated", flag: "zz-flag", run: () => true });
});

let container: HTMLElement;
let root: Root;
let client: QueryClient;
let textarea: HTMLTextAreaElement;
const textareaRef = { current: null as HTMLTextAreaElement | null };
const result: { current: HookResult } = { current: undefined as unknown as HookResult };

function Probe(props: Props) {
  result.current = useSlashAutocomplete({ ...props, textareaRef });
  return null;
}

function render(props: Props) {
  act(() => root.render(h(QueryClientProvider, { client }, h(Probe, props))));
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

// Type into the textarea and re-parse, the way the slot's onChange does.
function typeText(value: string, caret = value.length) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = caret;
  act(() => result.current.refreshSlash());
}

const key = (k: string) => ({ key: k, preventDefault: vi.fn() }) as unknown as React.KeyboardEvent<HTMLTextAreaElement> & {
  preventDefault: ReturnType<typeof vi.fn>;
};

const spoke = (name: string): ChatMessage =>
  ({ id: `m-${name}`, role: "assistant", content: "hi", author: { name } }) as unknown as ChatMessage;

let props: Props;

beforeEach(async () => {
  vi.spyOn(api, "chatCommands").mockResolvedValue({
    commands: [
      { name: "zzserver", kind: "skill", description: "a server skill", usage: "/zzserver" },
      { name: "zzshared", kind: "skill", description: "server copy", usage: "/zzshared" },
    ],
  });
  vi.spyOn(api, "chatMentions").mockResolvedValue({
    mentions: [
      { name: "alpha", kind: "a2a", description: "first", usage: "@alpha" },
      { name: "beta", kind: "a2a", description: "second", usage: "@beta" },
    ],
  } as Awaited<ReturnType<typeof api.chatMentions>>);
  vi.spyOn(api, "flags").mockResolvedValue({ flags: [] } as unknown as Awaited<ReturnType<typeof api.flags>>);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  textarea = document.createElement("textarea");
  document.body.appendChild(textarea);
  textareaRef.current = textarea;
  root = createRoot(container);
  clientRun.mockClear();
  props = { session: { messages: [] }, draft: "", setDraft: vi.fn(), runClientSlash: vi.fn(() => false) };
  render(props);
  await flush();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  textarea.remove();
  client.clear();
  vi.restoreAllMocks();
});

describe("useSlashAutocomplete — matching", () => {
  it("is closed with no token under the caret", () => {
    typeText("hello");
    expect(result.current.slashActive).toBe(false);
    expect(result.current.slashMatches).toEqual([]);
  });

  it("lists client commands first, then server skills, deduped by token (client wins)", () => {
    typeText("/zz");
    const names = result.current.slashMatches.map((c) => c.name);
    expect(names).toEqual(["zzlocal", "zzshared", "zzserver"]);
    expect(result.current.slashMatches.find((c) => c.name === "zzshared")?.description).toBe(
      "client twin of a server skill",
    );
    expect(result.current.slashSigil).toBe("/");
    expect(result.current.slashActive).toBe(true);
  });

  it("hides a flag-tagged command while its flag is off", () => {
    typeText("/zzflag");
    expect(result.current.slashMatches).toEqual([]);
  });

  it("opens mid-input on a token at the caret (#1530)", () => {
    typeText("please /zzl then", "please /zzl".length);
    expect(result.current.slashMatches.map((c) => c.name)).toEqual(["zzlocal"]);
  });

  it("tracks caret moves through the native keyup listener", () => {
    textarea.value = "/zzserver";
    textarea.selectionStart = textarea.selectionEnd = 9;
    act(() => textarea.dispatchEvent(new Event("keyup")));
    expect(result.current.slashMatches.map((c) => c.name)).toEqual(["zzserver"]);
  });

  it("fills an `@` token from the mention roster, speakers first (#3049)", () => {
    render({ ...props, session: { messages: [spoke("beta")] } });
    typeText("@");
    expect(result.current.slashSigil).toBe("@");
    expect(result.current.slashMatches.map((c) => c.name)).toEqual(["beta", "alpha"]);
  });

  it("exposes the server command list and the flag predicate for runClientSlash", () => {
    expect(result.current.commands.map((c) => c.name)).toEqual(["zzserver", "zzshared"]);
    expect(result.current.flagOn("zz-flag")).toBe(false);
  });
});

describe("useSlashAutocomplete — keyboard selection", () => {
  it("ignores keys while the popover is closed", () => {
    typeText("plain");
    const e = key("ArrowDown");
    expect(result.current.onSlashKeyDown(e)).toBe(false);
    expect(e.preventDefault).not.toHaveBeenCalled();
  });

  it("↓ / ↑ move the selection and wrap", () => {
    typeText("/zz");
    expect(result.current.slashSel).toBe(0);
    act(() => void result.current.onSlashKeyDown(key("ArrowDown")));
    expect(result.current.slashSel).toBe(1);
    act(() => void result.current.onSlashKeyDown(key("ArrowUp")));
    act(() => void result.current.onSlashKeyDown(key("ArrowUp")));
    expect(result.current.slashSel).toBe(2); // wrapped to the last of three
    act(() => void result.current.onSlashKeyDown(key("ArrowDown")));
    expect(result.current.slashSel).toBe(0);
  });

  it("Escape dismisses the menu until the input changes", () => {
    typeText("/zz");
    const e = key("Escape");
    let took = false;
    act(() => void (took = result.current.onSlashKeyDown(e)));
    expect(took).toBe(true);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(result.current.slashActive).toBe(false);
    act(() => result.current.setSlashDismissed(false)); // the slot's onChange re-opens
    expect(result.current.slashActive).toBe(true);
  });

  it("Enter on a server skill inserts `/name ` in place of the token", () => {
    const setDraft = vi.fn();
    render({ ...props, draft: "go /zzs now", setDraft });
    typeText("go /zzs now", "go /zzs".length);
    // matches: zzshared (client), zzserver — pick zzserver
    act(() => void result.current.onSlashKeyDown(key("ArrowDown")));
    act(() => void result.current.onSlashKeyDown(key("Enter")));
    expect(setDraft).toHaveBeenCalledWith("go /zzserver  now");
    expect(result.current.slashActive).toBe(false);
  });

  it("Tab on a client command runs it and drops just its token", () => {
    const setDraft = vi.fn();
    const runClientSlash = vi.fn(() => true);
    render({ ...props, draft: "x /zzl", setDraft, runClientSlash });
    typeText("x /zzl");
    act(() => void result.current.onSlashKeyDown(key("Tab")));
    expect(runClientSlash).toHaveBeenCalledWith("zzlocal");
    expect(setDraft).toHaveBeenCalledWith("x ");
    expect(result.current.slashActive).toBe(false);
  });

  it("completing an `@` mention never runs a client command", () => {
    const setDraft = vi.fn();
    const runClientSlash = vi.fn(() => true);
    render({ ...props, draft: "@al", setDraft, runClientSlash });
    typeText("@al");
    act(() => result.current.completeCommand(result.current.slashMatches[0]));
    expect(runClientSlash).not.toHaveBeenCalled();
    expect(setDraft).toHaveBeenCalledWith("@alpha ");
  });

  it("setSlashCtx(null) closes the popover (send clears it with the draft, #2492)", () => {
    typeText("/zz");
    act(() => {
      result.current.setSlashCtx(null);
      result.current.setSlashIndex(0);
    });
    expect(result.current.slashActive).toBe(false);
  });
});
