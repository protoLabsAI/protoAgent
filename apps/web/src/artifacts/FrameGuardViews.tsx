// The visible half of the frame focus guard (frameFocusGuard.ts, ADR 0118 / #4076): the
// click-to-activate shield over an inactive frame, and the placeholder a quarantined frame is
// replaced by. Shared by the inline artifact host and the frame-component host.

import type { FrameGuard } from "./frameFocusGuard";

/** A transparent click-catcher over an INACTIVE frame. The frame keeps running underneath (charts
 *  and animations stay live); the operator's first press lands here, in console DOM, and
 *  activates the frame — a press inside a cross-origin frame would never reach the host. Not
 *  focusable and hidden from assistive tech: keyboard users Tab straight into the frame. */
export function FrameActivationShield({ guard }: { guard: FrameGuard }) {
  if (guard.active || guard.quarantined) return null;
  return (
    <div
      className="frame-guard__shield"
      data-testid="frame-guard-shield"
      aria-hidden="true"
      onPointerDown={(e) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        // Cancel the press's own focus move to the page body, which would deactivate the frame
        // again (WebKit performs it even after the frame took focus here).
        e.preventDefault();
        guard.activate();
      }}
      onMouseDown={(e) => e.preventDefault()}
    >
      <span className="frame-guard__hint">Click to interact</span>
    </div>
  );
}

/** What a quarantined frame renders instead: why it stopped, and a way to run it anyway. */
export function FrameQuarantineNotice({
  what,
  height,
  onRun,
}: {
  what: "artifact" | "component";
  height?: number;
  onRun: () => void;
}) {
  return (
    <div className="frame-guard__quarantine" data-testid="frame-quarantine" role="status" style={{ minHeight: height }}>
      <span className="frame-guard__quarantine-msg">{`This ${what} tried to take keyboard focus.`}</span>
      <button type="button" className="frame-guard__run" data-testid="frame-quarantine-run" onClick={onRun}>
        Run it
      </button>
    </div>
  );
}
