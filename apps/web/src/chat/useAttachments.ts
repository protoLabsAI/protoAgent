import { useRef, useState } from "react";
import type React from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { messageId } from "./messageId";
import { filesFromTransfer, isLargePaste, pastedTextFile } from "./paste";

// Read a File to bare base64 (no `data:…;base64,` prefix) — the proto Part `raw`
// (bytes) field for a native-vision image.
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result || "");
      const comma = s.indexOf(",");
      resolve(comma >= 0 ? s.slice(comma + 1) : s);
    };
    reader.onerror = () => reject(reader.error || new Error("file read failed"));
    reader.readAsDataURL(file);
  });
}

// A file being attached to the next message. Uploaded to /api/knowledge/attach on
// pick; `context` is the backend's ready-to-prepend block (full text or lede).
export type PendingAttachment = {
  id: string;
  name: string;
  kind: "file" | "image";
  status: "uploading" | "ready" | "error";
  context?: string;
  mode?: "inline" | "indexed";
  error?: string;
  // Native-vision images skip the pipeline: their base64 + mime ride the turn as
  // a multimodal A2A part straight to the model (no `context`).
  native?: boolean;
  b64?: string;
  mime?: string;
};

export type UseAttachmentsOptions = {
  sessionId: string;
  onError: (message: string) => void;
  // The active chat model accepts images natively (runtime.model.vision).
  visionModel: boolean;
  // A configured vision model can DESCRIBE images for a text-only chat model (#1381).
  imageDescribe: boolean;
};

// The composer's pending-attachment tray (#3850, extracted from ChatSessionSlot): the
// state, the upload pipeline, and the add paths (file picker, drop, paste). Every handler
// is a fresh per-render closure over the options, exactly as it was inline in the slot —
// so an upload started on a render reads that render's `visionModel`/`imageDescribe`.
// The slot keeps READING the state (send, canSend, the unload guard) and clears it on send
// through the returned `setAttachments`; there is exactly one copy of the state.
export function useAttachments({ sessionId, onError, visionModel, imageDescribe }: UseAttachmentsOptions) {
  // Pending file attachments. Each is uploaded to /api/knowledge/attach on pick;
  // the backend tiers it (inline small / index large) and returns a `context`
  // block we prepend to the SENT message (not the visible bubble) on send.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  async function uploadAttachment(file: File) {
    const id = messageId();
    const kind: "file" | "image" = file.type.startsWith("image/") ? "image" : "file";
    setAttachments((a) => [...a, { id, name: file.name, kind, status: "uploading" }]);

    // Images always ride the turn natively as multimodal parts: a vision model
    // sees them directly, and on a text-only model the server still bridges them
    // into the media store so image tools can act on them by id (#1969) — the
    // old hard error (#1374) is gone.
    if (kind === "image") {
      try {
        const b64 = await fileToBase64(file);
        setAttachments((a) =>
          a.map((x) =>
            x.id === id
              ? { ...x, status: "ready", native: true, b64, mime: file.type || "image/png", mode: "inline" }
              : x,
          ),
        );
      } catch (e) {
        const msg = errMsg(e);
        setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: "error", error: msg } : x)));
        onError(`Couldn't read ${file.name}: ${msg}`);
        return;
      }
      // A configured describe model (#1381) still adds a textual description for a
      // text-only chat model — best-effort context alongside the native part; a
      // describe failure never sinks the already-ready attachment.
      if (visionModel || !imageDescribe) return;
      try {
        const form = new FormData();
        form.append("file", file);
        form.append("session_id", sessionId);
        const r = await api.attachToChat(form);
        if (r.enabled && r.context) {
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, context: r.context } : x)));
        }
      } catch {
        // native attachment already succeeded; description is additive
      }
      return;
    }

    try {
      const form = new FormData();
      form.append("file", file);
      form.append("session_id", sessionId);
      const r = await api.attachToChat(form);
      if (!r.enabled || !r.context) throw new Error("attachment not accepted");
      setAttachments((a) =>
        a.map((x) => (x.id === id ? { ...x, status: "ready", context: r.context, mode: r.mode } : x)),
      );
    } catch (e) {
      const msg = errMsg(e);
      setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: "error", error: msg } : x)));
      onError(`Couldn't attach ${file.name}: ${msg}`);
    }
  }

  function removeAttachment(id: string) {
    setAttachments((a) => a.filter((x) => x.id !== id));
  }

  // Drag-and-drop onto the composer: claim the drag only when it carries files.
  function onDragOver(e: React.DragEvent) {
    if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
  }

  function onDrop(e: React.DragEvent) {
    const files = filesFromTransfer(e.dataTransfer);
    if (files.length) {
      e.preventDefault();
      files.forEach((f) => void uploadAttachment(f));
    }
  }

  function onPaste(e: React.ClipboardEvent) {
    // Paste-to-attach (the DS onPaste seam). Clipboard files — incl.
    // IMAGES/screenshots that some browsers expose only via items[] —
    // become attachments.
    const files = filesFromTransfer(e.clipboardData);
    if (files.length) {
      e.preventDefault();
      files.forEach((f) => void uploadAttachment(f));
      return;
    }
    // A large text paste becomes a removable attachment pill (routed
    // through the attach pipeline → tiered inline/indexed) instead of
    // flooding the field; small pastes fall through to the textarea.
    const text = e.clipboardData?.getData("text/plain") ?? "";
    if (isLargePaste(text)) {
      e.preventDefault();
      void uploadAttachment(pastedTextFile(text));
    }
  }

  // The composer's 📎 button opens the hidden file input…
  function openFilePicker() {
    fileInputRef.current?.click();
  }

  // …whose change event uploads every picked file.
  function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    files.forEach((f) => void uploadAttachment(f));
    e.target.value = ""; // allow re-picking the same file
  }

  return {
    attachments,
    setAttachments,
    fileInputRef,
    uploadAttachment,
    removeAttachment,
    onDragOver,
    onDrop,
    onPaste,
    openFilePicker,
    onFileInputChange,
  };
}
