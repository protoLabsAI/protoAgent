// useAttachments (#3850) — the composer's pending-attachment tray, extracted from
// ChatSessionSlot. Drives the hook through a minimal createRoot/act renderHook (the console
// has no testing-library dep — same pattern as the other UI suites) and pins every add path
// the slot wires: the upload pipeline (file / native image / describe / failures), remove,
// drop, paste (files and large text), and the hidden file picker.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { LARGE_PASTE_CHARS } from "./paste";
import { fileToBase64, useAttachments, type UseAttachmentsOptions } from "./useAttachments";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type HookResult = ReturnType<typeof useAttachments>;

let container: HTMLElement;
let root: Root;
let result: { current: HookResult };
let Probe: (props: UseAttachmentsOptions) => null;

function renderHook(opts: UseAttachmentsOptions) {
  result = { current: undefined as unknown as HookResult };
  Probe = function Probe(props: UseAttachmentsOptions) {
    result.current = useAttachments(props);
    return null;
  };
  act(() => root.render(h(Probe, opts)));
  return result;
}

// Re-render the SAME mounted Probe with new options (state and refs survive) — the way
// the slot re-renders the hook when the active model changes.
function rerender(opts: UseAttachmentsOptions) {
  act(() => root.render(h(Probe, opts)));
}

// Let the FileReader / mocked-fetch promise chains settle inside act.
async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

const baseOpts = (over: Partial<UseAttachmentsOptions> = {}): UseAttachmentsOptions => ({
  sessionId: "sess-1",
  onError: vi.fn(),
  visionModel: false,
  imageDescribe: false,
  ...over,
});

const textFile = (name = "notes.md") => new File(["hello"], name, { type: "text/markdown" });
const imageFile = (name = "shot.png") => new File([new Uint8Array([1, 2, 3])], name, { type: "image/png" });

// A DataTransfer-ish stand-in: filesFromTransfer reads `items` then `files`.
function transfer(files: File[], text = "") {
  return {
    items: files.map((f) => ({ kind: "file", getAsFile: () => f })),
    files,
    types: files.length ? ["Files"] : ["text/plain"],
    getData: (t: string) => (t === "text/plain" ? text : ""),
  };
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("fileToBase64", () => {
  it("returns bare base64 with the data: prefix stripped", async () => {
    const b64 = await fileToBase64(new File(["hi"], "a.txt", { type: "text/plain" }));
    expect(b64).toBe(btoa("hi"));
  });
});

describe("useAttachments — upload pipeline", () => {
  it("starts empty", () => {
    const r = renderHook(baseOpts());
    expect(r.current.attachments).toEqual([]);
  });

  it("uploads a file through /attach: uploading → ready with the context + mode", async () => {
    type AttachAnswer = Awaited<ReturnType<typeof api.attachToChat>>;
    let resolve!: (v: AttachAnswer) => void;
    const spy = vi.spyOn(api, "attachToChat").mockReturnValue(new Promise<AttachAnswer>((r) => (resolve = r)));
    const r = renderHook(baseOpts());
    act(() => void r.current.uploadAttachment(textFile()));
    expect(r.current.attachments).toMatchObject([{ name: "notes.md", kind: "file", status: "uploading" }]);
    const form = spy.mock.calls[0][0];
    expect(form.get("session_id")).toBe("sess-1");
    expect((form.get("file") as File).name).toBe("notes.md");
    resolve({ enabled: true, context: "CTX", mode: "indexed" });
    await flush();
    expect(r.current.attachments).toMatchObject([{ status: "ready", context: "CTX", mode: "indexed" }]);
  });

  it("marks the pill failed and reports when the upload throws", async () => {
    vi.spyOn(api, "attachToChat").mockRejectedValue(new Error("boom"));
    const onError = vi.fn();
    const r = renderHook(baseOpts({ onError }));
    act(() => void r.current.uploadAttachment(textFile()));
    await flush();
    expect(r.current.attachments).toMatchObject([{ status: "error", error: "boom" }]);
    expect(onError).toHaveBeenCalledWith("Couldn't attach notes.md: boom");
  });

  it("treats a disabled / context-less answer as not accepted", async () => {
    vi.spyOn(api, "attachToChat").mockResolvedValue({ enabled: false } as Awaited<
      ReturnType<typeof api.attachToChat>
    >);
    const onError = vi.fn();
    const r = renderHook(baseOpts({ onError }));
    act(() => void r.current.uploadAttachment(textFile()));
    await flush();
    expect(r.current.attachments[0].status).toBe("error");
    expect(onError).toHaveBeenCalledWith("Couldn't attach notes.md: attachment not accepted");
  });

  it("rides an image natively (base64 + mime) without the pipeline by default", async () => {
    const spy = vi.spyOn(api, "attachToChat");
    const r = renderHook(baseOpts());
    act(() => void r.current.uploadAttachment(imageFile()));
    await flush();
    expect(r.current.attachments).toMatchObject([
      { kind: "image", status: "ready", native: true, b64: btoa("\x01\x02\x03"), mime: "image/png", mode: "inline" },
    ]);
    expect(spy).not.toHaveBeenCalled();
  });

  it("adds a describe-model context to a native image on a text-only model (#1381)", async () => {
    const spy = vi.spyOn(api, "attachToChat").mockResolvedValue({ enabled: true, context: "a cat" } as Awaited<
      ReturnType<typeof api.attachToChat>
    >);
    const r = renderHook(baseOpts({ imageDescribe: true, visionModel: false }));
    act(() => void r.current.uploadAttachment(imageFile()));
    await flush();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(r.current.attachments).toMatchObject([{ status: "ready", native: true, context: "a cat" }]);
  });

  it("skips the describe call when the chat model sees images itself", async () => {
    const spy = vi.spyOn(api, "attachToChat");
    const r = renderHook(baseOpts({ imageDescribe: true, visionModel: true }));
    act(() => void r.current.uploadAttachment(imageFile()));
    await flush();
    expect(spy).not.toHaveBeenCalled();
    expect(r.current.attachments[0]).toMatchObject({ status: "ready", native: true });
  });

  it("reads the CURRENT visionModel: switching to a vision model after first render skips describe (#3855)", async () => {
    const spy = vi.spyOn(api, "attachToChat");
    const r = renderHook(baseOpts({ imageDescribe: true, visionModel: false }));
    rerender(baseOpts({ imageDescribe: true, visionModel: true }));
    act(() => void r.current.uploadAttachment(imageFile()));
    await flush();
    expect(spy).not.toHaveBeenCalled();
    expect(r.current.attachments).toHaveLength(1);
    expect(r.current.attachments[0]).toMatchObject({ status: "ready", native: true });
    expect(r.current.attachments[0].context).toBeUndefined();
  });

  it("a failed describe never sinks the ready native image", async () => {
    vi.spyOn(api, "attachToChat").mockRejectedValue(new Error("describe down"));
    const onError = vi.fn();
    const r = renderHook(baseOpts({ imageDescribe: true, onError }));
    act(() => void r.current.uploadAttachment(imageFile()));
    await flush();
    expect(r.current.attachments[0]).toMatchObject({ status: "ready", native: true });
    expect(r.current.attachments[0].context).toBeUndefined();
    expect(onError).not.toHaveBeenCalled();
  });

  it("removeAttachment drops just that pill; setAttachments clears the tray (send)", async () => {
    vi.spyOn(api, "attachToChat").mockResolvedValue({ enabled: true, context: "C", mode: "inline" } as Awaited<
      ReturnType<typeof api.attachToChat>
    >);
    const r = renderHook(baseOpts());
    act(() => {
      void r.current.uploadAttachment(textFile("a.md"));
      void r.current.uploadAttachment(textFile("b.md"));
    });
    await flush();
    const [a] = r.current.attachments;
    act(() => r.current.removeAttachment(a.id));
    expect(r.current.attachments.map((x) => x.name)).toEqual(["b.md"]);
    act(() => r.current.setAttachments([]));
    expect(r.current.attachments).toEqual([]);
  });
});

describe("useAttachments — add paths", () => {
  beforeEach(() => {
    vi.spyOn(api, "attachToChat").mockResolvedValue({ enabled: true, context: "C", mode: "inline" } as Awaited<
      ReturnType<typeof api.attachToChat>
    >);
  });

  it("onDragOver claims only a drag that carries files", () => {
    const r = renderHook(baseOpts());
    const withFiles = { dataTransfer: transfer([textFile()]), preventDefault: vi.fn() };
    const textOnly = { dataTransfer: transfer([]), preventDefault: vi.fn() };
    r.current.onDragOver(withFiles as never);
    r.current.onDragOver(textOnly as never);
    expect(withFiles.preventDefault).toHaveBeenCalled();
    expect(textOnly.preventDefault).not.toHaveBeenCalled();
  });

  it("onDrop uploads every dropped file", async () => {
    const r = renderHook(baseOpts());
    const e = { dataTransfer: transfer([textFile("a.md"), textFile("b.md")]), preventDefault: vi.fn() };
    act(() => r.current.onDrop(e as never));
    await flush();
    expect(e.preventDefault).toHaveBeenCalled();
    expect(r.current.attachments.map((x) => x.name)).toEqual(["a.md", "b.md"]);
  });

  it("onDrop with no files leaves the event alone", () => {
    const r = renderHook(baseOpts());
    const e = { dataTransfer: transfer([]), preventDefault: vi.fn() };
    act(() => r.current.onDrop(e as never));
    expect(e.preventDefault).not.toHaveBeenCalled();
    expect(r.current.attachments).toEqual([]);
  });

  it("onPaste turns clipboard files into attachments", async () => {
    const r = renderHook(baseOpts());
    const e = { clipboardData: transfer([imageFile()]), preventDefault: vi.fn() };
    act(() => r.current.onPaste(e as never));
    await flush();
    expect(e.preventDefault).toHaveBeenCalled();
    expect(r.current.attachments).toMatchObject([{ name: "shot.png", kind: "image" }]);
  });

  it("onPaste with clipboard files AND large text attaches only the files (#3855)", async () => {
    const r = renderHook(baseOpts());
    const e = {
      clipboardData: transfer([imageFile()], "x".repeat(LARGE_PASTE_CHARS + 1)),
      preventDefault: vi.fn(),
    };
    act(() => r.current.onPaste(e as never));
    await flush();
    expect(e.preventDefault).toHaveBeenCalled();
    expect(r.current.attachments).toHaveLength(1);
    expect(r.current.attachments).toMatchObject([{ name: "shot.png", kind: "image" }]);
    expect(api.attachToChat).not.toHaveBeenCalled(); // the text was never converted/uploaded
  });

  it("onPaste turns a LARGE text paste into a 'Pasted text.txt' pill", async () => {
    const r = renderHook(baseOpts());
    const e = { clipboardData: transfer([], "x".repeat(LARGE_PASTE_CHARS + 1)), preventDefault: vi.fn() };
    act(() => r.current.onPaste(e as never));
    await flush();
    expect(e.preventDefault).toHaveBeenCalled();
    expect(r.current.attachments).toMatchObject([{ name: "Pasted text.txt", kind: "file", status: "ready" }]);
  });

  it("onPaste lets a small text paste fall through to the textarea", () => {
    const r = renderHook(baseOpts());
    const e = { clipboardData: transfer([], "short"), preventDefault: vi.fn() };
    act(() => r.current.onPaste(e as never));
    expect(e.preventDefault).not.toHaveBeenCalled();
    expect(r.current.attachments).toEqual([]);
  });

  it("openFilePicker clicks the hidden input; onFileInputChange uploads and resets it", async () => {
    const r = renderHook(baseOpts());
    const input = document.createElement("input");
    const click = vi.spyOn(input, "click").mockImplementation(() => {});
    r.current.fileInputRef.current = input;
    r.current.openFilePicker();
    expect(click).toHaveBeenCalled();

    const target = { files: [textFile("picked.md")], value: "C:\\fakepath\\picked.md" };
    act(() => r.current.onFileInputChange({ target } as never));
    await flush();
    expect(r.current.attachments.map((x) => x.name)).toEqual(["picked.md"]);
    expect(target.value).toBe(""); // allow re-picking the same file
  });
});
