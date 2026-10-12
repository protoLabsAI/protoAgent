// #4076: the send confirm chip's "Always allow" checkbox once sat alone mid-row while its label
// wrapped onto two lines at the far right — theme.css's global `input { width: 100% }` stretched a
// bare checkbox across the trust row. The chip is now the DS ConfirmChip, whose trust control is
// the DS Checkbox: ONE <label class="pl-checkbox"> holding a visually-hidden input (class-scoped
// `width: 0`, which outranks the global element rule) plus a drawn box and the label text. jsdom
// does no layout, so this pins that structure for BOTH chip hosts' sources (the browser check
// lives in the PR's screenshots).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BridgeNoticeView } from "./FrameProposalChip";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("frame proposal chip: the trust checkbox stays with its label (#4076)", () => {
  for (const [prefix, source] of [["artifact", "artifact"], ["component", "component"]] as const) {
    for (const action of ["send", "open"] as const) {
      it(`${prefix} ${action}: the checkbox and its label are one DS Checkbox unit`, () => {
        const text = action === "open" ? "https://docs.example.com/x" : "hello";
        act(() =>
          root.render(
            h(BridgeNoticeView, {
              notice: { kind: "confirm", action, cid: 1, text },
              testIdPrefix: prefix,
              source,
              onAccept: () => {},
              onDismiss: () => {},
              onClearRejection: () => {},
            }),
          ),
        );
        const input = container.querySelector<HTMLInputElement>(".pl-confirm-chip__trust input[type='checkbox']");
        expect(input).not.toBeNull();
        expect(input!.className).toBe("pl-checkbox__input");
        const label = input!.closest("label");
        expect(label?.classList.contains("pl-checkbox")).toBe(true);
        const trust = action === "open" ? `Always allow links from this ${source}` : `Always allow from this ${source}`;
        expect(label?.textContent).toContain(trust);
        expect(input!.checked).toBe(false);
      });
    }
  }
});
