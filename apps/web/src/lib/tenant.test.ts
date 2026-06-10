// Tenant guard (pure check): a different backend uid on the same origin drops the
// previous tenant's persisted chat view; same/first uid is a no-op.
import { beforeEach, describe, expect, it } from "vitest";

import { tenantCheck } from "../lib/tenant";

describe("tenantCheck", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("first visit stores the uid and keeps state", () => {
    localStorage.setItem("protoagent.chat.sessions", "{}");
    expect(tenantCheck("uid-a")).toBe("ok");
    expect(localStorage.getItem("protoagent.tenant.uid")).toBe("uid-a");
    expect(localStorage.getItem("protoagent.chat.sessions")).toBe("{}");
  });

  it("same uid (restart/upgrade of the same agent) keeps state", () => {
    localStorage.setItem("protoagent.tenant.uid", "uid-a");
    localStorage.setItem("protoagent.chat.sessions:ava", "{}");
    expect(tenantCheck("uid-a")).toBe("ok");
    expect(localStorage.getItem("protoagent.chat.sessions:ava")).toBe("{}");
  });

  it("a different uid clears every slug's chat view + the watcher state", () => {
    localStorage.setItem("protoagent.tenant.uid", "uid-a");
    localStorage.setItem("protoagent.chat.sessions", "{}");
    localStorage.setItem("protoagent.chat.sessions:ava", "{}");
    localStorage.setItem("protoagent.ui", "{layout}"); // layout survives — cosmetic
    sessionStorage.setItem("protoagent.turnwatch.notified", "[]");

    expect(tenantCheck("uid-b")).toBe("switched");
    expect(localStorage.getItem("protoagent.chat.sessions")).toBeNull();
    expect(localStorage.getItem("protoagent.chat.sessions:ava")).toBeNull();
    expect(localStorage.getItem("protoagent.ui")).toBe("{layout}");
    expect(sessionStorage.getItem("protoagent.turnwatch.notified")).toBeNull();
    expect(sessionStorage.getItem("protoagent.tenant.switched")).toBe("1");
    expect(localStorage.getItem("protoagent.tenant.uid")).toBe("uid-b");
  });

  it("no uid from the backend (older server) never clears", () => {
    localStorage.setItem("protoagent.tenant.uid", "uid-a");
    localStorage.setItem("protoagent.chat.sessions", "{}");
    expect(tenantCheck(undefined)).toBe("ok");
    expect(localStorage.getItem("protoagent.chat.sessions")).toBe("{}");
  });
});
