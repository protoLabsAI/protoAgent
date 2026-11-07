import { afterEach, expect, it, vi } from "vitest";
import { openArtifactEvent } from "./ArtifactOpenWatch";

vi.mock("../lib/api", () => ({ apiUrl: (path: string) => `/agents/test${path}` }));
afterEach(() => vi.restoreAllMocks());
const path = `/plugins/artifact/media/artifact/a-123/blob?version=2&exp=1900000000&t=${"a".repeat(64)}`;

it("opens a ticketed artifact in a new tab for the matching chat, with fleet routing", () => {
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  openArtifactEvent({ session_id: "chat", path }, "chat");
  expect(open).toHaveBeenCalledWith(`/agents/test${path}`, "_blank", "noopener,noreferrer");
});

it("ignores other chats, absent attendance, arbitrary URLs and bearer queries", () => {
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  openArtifactEvent({ session_id: "other", path }, "chat");
  openArtifactEvent({ session_id: "chat", path }, null);
  for (const bad of ["https://example.com", "javascript:alert(1)", path + "&token=secret", "/api/config"]) {
    openArtifactEvent({ session_id: "chat", path: bad }, "chat");
  }
  expect(open).not.toHaveBeenCalled();
});
