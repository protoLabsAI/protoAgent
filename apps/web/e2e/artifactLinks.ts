import type { Frame, Page } from "@playwright/test";

// Code-linked mermaid diagrams (ADR 0038 amendment): a canned store whose mermaid versions carry
// `links` — the validated {project, path, line, end_line, note} targets the server stored with the
// version. Shared by the shell-level spec (artifact-mermaid-links.spec.ts) and the console spec.

export const SEQ = [
  "sequenceDiagram",
  "  participant U as User",
  "  participant S as Server",
  "  U->>S: ask(question)",
  "  S->>S: run tools<br/>(loop)",
  "  S-->>U: answer",
].join("\n");

export const FLOW = ["flowchart TD", "  A[Start] --> B{Check}", "  B -->|yes| C[Done]"].join("\n");

export const SEQ_LINKS = {
  "msg:1": { project: "demo", path: "src/agent.py", line: 12, end_line: 20, note: "ask() entry point" },
  "msg:2": { project: "demo", path: "src/tools.py", line: 40, end_line: 44, note: "the tool loop" },
  "participant:Server": { project: "demo", path: "src/server.py", line: 3, end_line: 3, note: "" },
  // A note carrying markup must reach the screen as TEXT (tooltip + Links list).
  "msg:answer": {
    project: "demo",
    path: "src/agent.py",
    line: 30,
    end_line: 30,
    note: '<img src=x onerror="window.__xss=1"></script>',
  },
  "ghost-node": { project: "demo", path: "src/agent.py", line: 1, end_line: 1, note: "matches nothing" },
};

export const LINKS_STORE = {
  current: "art-seq",
  artifacts: [
    {
      id: "art-seq",
      kind: "mermaid",
      title: "ask() flow",
      versions: [
        // v1 predates the links: links live WITH a version, so it shows none while v2 does.
        { code: SEQ.replace("answer", "first answer"), ts: 1, by: "agent" },
        { code: SEQ, ts: 2, by: "agent", links: SEQ_LINKS },
      ],
      version_count: 2,
      created: 1,
      updated: 2,
    },
    {
      id: "art-flow",
      kind: "mermaid",
      title: "Flow",
      versions: [
        {
          code: FLOW,
          ts: 1,
          by: "agent",
          links: { B: { project: "demo", path: "src/check.py", line: 7, end_line: 9, note: "the check" } },
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
    {
      id: "art-big",
      kind: "svg",
      title: "Big svg",
      versions: [
        {
          code: '<svg id="big" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4000 3000"><rect width="4000" height="3000" fill="#234"/><circle cx="2000" cy="1500" r="400" fill="#9b87f2"/></svg>',
          ts: 1,
          by: "agent",
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
    {
      id: "art-md",
      kind: "markdown",
      title: "Doc",
      versions: [{ code: "# Doc\n\n```mermaid\n" + FLOW + "\n```\n\nafter", ts: 1, by: "agent" }],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

export async function routeLinksStore(page: Page) {
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: LINKS_STORE }));
}

export async function artifactFrame(page: Page | Frame, hostSel = "#frame"): Promise<Frame> {
  const handle = await page.locator(hostSel).elementHandle();
  const f = handle ? await handle.contentFrame() : null;
  if (!f) throw new Error("no artifact frame");
  return f;
}
