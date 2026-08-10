// #2474 — the two global "Focus chat composer" bindings must navigate to the
// chat surface before bumping the focus nonce: the composer only exists there,
// so from Knowledge/Settings the shortcut used to fire into a hidden panel.
import { beforeAll, describe, expect, it } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { useKbIntents } from "./intents";
import { useUI } from "../state/uiStore";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

function runBinding(id: string) {
  const b = registeredKeybindings().find((k) => k.id === id);
  expect(b, `binding ${id} registered`).toBeTruthy();
  b!.run(new KeyboardEvent("keydown"));
}

describe.each(["composer.focus", "focus.chat"])("%s from a non-chat surface", (id) => {
  it("navigates to chat AND requests composer focus", () => {
    useUI.setState({ surface: "knowledge" });
    const nonceBefore = useKbIntents.getState().composerFocusNonce;
    runBinding(id);
    expect(useUI.getState().surface).toBe("chat");
    expect(useKbIntents.getState().composerFocusNonce).toBe(nonceBefore + 1);
  });
});
