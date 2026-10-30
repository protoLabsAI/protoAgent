// #4645 console half: a steer carries the tray's images in the request body — as the
// `[mime, data-URI]` pairs the steer route's `_coerce_attachment_images` takes — but only
// when there are any, so a text-only steer still sends exactly `{id, text}` as before.
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";

function captureBody() {
  const bodies: Array<Record<string, unknown>> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      bodies.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ ok: true, id: "m1", pending: 1 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return bodies;
}

afterEach(() => vi.unstubAllGlobals());

describe("steerChat: optional image attachments", () => {
  it("sends exactly {id, text} for a text-only steer", async () => {
    const bodies = captureBody();
    await api.steerChat("chat-1", "m1", "go left");
    expect(bodies[0]).toEqual({ id: "m1", text: "go left" });
  });

  it("omits images when the list is empty", async () => {
    const bodies = captureBody();
    await api.steerChat("chat-1", "m1", "go left", []);
    expect(bodies[0]).toEqual({ id: "m1", text: "go left" });
  });

  it("carries images as [mime, data-URI] pairs", async () => {
    const bodies = captureBody();
    await api.steerChat("chat-1", "m1", "look", [{ b64: "AAAA", mime: "image/png", name: "shot.png" }]);
    expect(bodies[0]).toEqual({
      id: "m1",
      text: "look",
      images: [["image/png", "data:image/png;base64,AAAA"]],
    });
  });

  it("allows an image with an empty draft (the image is the message)", async () => {
    const bodies = captureBody();
    await api.steerChat("chat-1", "m1", "", [{ b64: "BBBB", mime: "image/jpeg", name: "p.jpg" }]);
    expect(bodies[0]).toEqual({
      id: "m1",
      text: "",
      images: [["image/jpeg", "data:image/jpeg;base64,BBBB"]],
    });
  });
});
