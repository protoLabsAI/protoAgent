// What the console's file pickers offer — ONE home for the chat composer's and the
// Knowledge page's `accept` lists (and the e2e specs that assert them), mirroring what
// the server's ingestion engine extracts (ingestion/engine.py). Only the native "browse"
// dialog filters by these; drag-and-drop and paste send anything, and the server answers
// 415 (unknown / legacy .doc) or 501 (a format whose optional library isn't installed).
// No React, no DOM: the specs under e2e/ import this directly.

/** Word .docx by MIME as well as extension — some pickers (iOS, Android) filter by type. */
export const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

const DOCUMENTS = [".txt", ".text", ".log", ".csv", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", DOCX_MIME];
const IMAGES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"];
const AUDIO = [".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"];
const VIDEO = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"];

/** Chat composer (`POST /api/knowledge/attach`) — images also ride the turn natively. */
export const CHAT_ATTACH_ACCEPT = [...DOCUMENTS, ...IMAGES, ...AUDIO, ...VIDEO].join(",");

/** Knowledge ▸ Add source (`POST /api/knowledge/ingest`). */
export const KNOWLEDGE_INGEST_ACCEPT = [...DOCUMENTS, ...AUDIO, ...VIDEO].join(",");
