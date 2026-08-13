import { describe, it, expect, afterEach } from "vitest";

import { closePublishDialog, getPublishDialogSessionId, openPublishDialog } from "./publishDialogStore";

afterEach(() => {
  closePublishDialog();
});

describe("publishDialogStore", () => {
  it("starts closed", () => {
    expect(getPublishDialogSessionId()).toBeNull();
  });

  it("open sets the session id", () => {
    openPublishDialog("s1");
    expect(getPublishDialogSessionId()).toBe("s1");
  });

  it("close clears it", () => {
    openPublishDialog("s1");
    closePublishDialog();
    expect(getPublishDialogSessionId()).toBeNull();
  });

  it("opening a different session replaces the current one", () => {
    openPublishDialog("s1");
    openPublishDialog("s2");
    expect(getPublishDialogSessionId()).toBe("s2");
  });
});
