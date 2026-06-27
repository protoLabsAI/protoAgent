// Full-screen document viewer (ADR 0062) — an extensible reader surface any feature can
// open with `openDocument({ title, content | load | render })`. First consumers: the chat
// background-agent report card + the Activity feed.
export { openDocument, closeDocument, useDocViewer } from "./store";
export { DocumentViewer } from "./DocumentViewer";
export type { DocumentSpec } from "./types";
