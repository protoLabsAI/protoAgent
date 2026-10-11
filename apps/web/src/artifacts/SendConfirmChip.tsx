// ADR 0118 D4 as amended by S19 (#4137): "the frame proposes; the user decides."
//
// The host side of a frame's send(text), shared by the inline artifact host (ArtifactRefChip) and
// the frame-component host (FrameComponentHost, S12) so both behave identically:
//   • every proposal runs through the frameBridge gates (content, busy, send rate, proposal rate);
//   • an untrusted source gets an inline CONFIRM CHIP in console DOM — `Send to chat: "…"` with
//     [Send] [Dismiss] and an "Always allow" checkbox. The frame cannot click host DOM, so nothing
//     posts without the operator (no gesture heuristic to fool — #4137's self-focus bypass);
//   • a trusted source ("Always allow", per browser) skips the chip but not the gates;
//   • a new proposal while the chip is open REPLACES its text (the older proposal's promise is
//     rejected) — chips never stack.
// openLink(url) goes through the SAME chip (#4076 F1) — "Open ‹host› in a new tab?" with
// [Open] [Dismiss] and "Always allow links" — behind the same gates (https + the operator's
// `artifact.open_link_origins`, one open per 2 s, the shared proposal window, one chip at a time).
// Link trust is a separate store from send trust. The tab opens from the chip's click (a real
// gesture, so pop-up blockers allow it), and the frame's Promise resolves only if it really
// opened: a blocked pop-up or a dismiss rejects with the reason.
// The hosts own the message plumbing (e.source checks, origin tags, replying to their frame);
// this owns the decision state and the chip UI.

import { useCallback, useId, useRef, useState } from "react";

import type { FrameBridge } from "./frameBridge";
import { loadOpenLinkOrigins } from "./openLinkOrigins";
import { openLinkInNewTab } from "./openLinkInNewTab";

/** What the frame proposed: a chat message (`send`) or a link to open (`open`, `text` = the URL). */
export type ProposalAction = "send" | "open";

/** What the host shows under its frame: the confirm chip, or a rejection the operator sees in
 *  place ("the agent is busy", a refused link). */
export type BridgeNotice =
  | { kind: "confirm"; action: ProposalAction; cid: number; text: string }
  | { kind: "rejected"; message: string };

export type FrameReply = (cid: number, result: { ok: boolean; text?: string; error?: string }) => void;

/** The chat a proposal posts into: `isBusy` for the gate, `post` runs the NORMAL origin-tagged
 *  send path (the host closes over its own origin tag). */
export type ProposalTarget = { isBusy: () => boolean; post: (text: string) => void };

/** Error strings the frame's send() Promise rejects with. */
export const SEND_DISMISSED_ERROR = "The user dismissed this message — it was not sent to chat.";
export const SEND_SUPERSEDED_ERROR = "Replaced by a newer message from this frame before the user answered.";
/** Error strings the frame's openLink() Promise rejects with. */
export const OPEN_LINK_DISMISSED_ERROR = "The user dismissed this link — it was not opened.";
export const OPEN_LINK_SUPERSEDED_ERROR = "Replaced by a newer request from this frame before the user answered.";
export const OPEN_LINK_BLOCKED_ERROR = "The browser blocked the new tab (pop-up blocker) — the link was not opened.";
export const OPEN_LINK_SETTINGS_ERROR = "Couldn't read the allowed link origins — the link was not opened.";

const supersededError = (action: ProposalAction) =>
  action === "open" ? OPEN_LINK_SUPERSEDED_ERROR : SEND_SUPERSEDED_ERROR;

export function useFrameSendGate({
  bridge,
  frameId,
  trustKey,
  reply,
  openLink = openLinkInNewTab,
  loadAllowOrigins = loadOpenLinkOrigins,
}: {
  bridge: FrameBridge;
  frameId: string;
  trustKey: string;
  reply: FrameReply;
  /** Opens the tab; true only if it really opened (injectable for tests). */
  openLink?: (url: string) => boolean;
  /** The operator's `artifact.open_link_origins` (injectable for tests). Rejects ⇒ refuse. */
  loadAllowOrigins?: () => Promise<readonly string[]>;
}) {
  const [notice, setNoticeState] = useState<BridgeNotice | null>(null);
  // Mirrored in a ref so the window `message` listener (a long-lived closure) and the chip
  // handlers always read the CURRENT notice, never a stale render's.
  const noticeRef = useRef<BridgeNotice | null>(null);
  const setNotice = useCallback((n: BridgeNotice | null) => {
    noticeRef.current = n;
    setNoticeState(n);
  }, []);
  const openChip = () => (noticeRef.current?.kind === "confirm" ? noticeRef.current : null);

  /** Show a rejection in place — unless a chip is open, which keeps priority (a stray refused
   *  request must not wipe a pending decision; the frame still learns the error). */
  const showRejection = useCallback(
    (message: string) => {
      if (!openChip()) setNotice({ kind: "rejected", message });
    },
    [setNotice],
  );

  /** A frame's send(text) proposal, correlated by `cid`. */
  const propose = useCallback(
    (cid: number, text: string, target: ProposalTarget) => {
      const verdict = bridge.checkSend({ frameId, trustKey, text, isBusy: target.isBusy });
      if (verdict.status === "rejected") {
        reply(cid, { ok: false, error: verdict.message });
        showRejection(verdict.message);
        return;
      }
      if (verdict.status === "ok") {
        // Trusted source — straight through (it already passed the content/busy/rate gates).
        target.post(verdict.text);
        reply(cid, { ok: true, text: verdict.text });
        if (!openChip()) setNotice(null);
        return;
      }
      // needs-confirm: (re)open the chip. A pending proposal is superseded, never stacked.
      const prev = openChip();
      if (prev) reply(prev.cid, { ok: false, error: supersededError(prev.action) });
      setNotice({ kind: "confirm", action: "send", cid, text: verdict.text });
    },
    [bridge, frameId, trustKey, reply, setNotice, showRejection],
  );

  /** Open `url` now; reply truthfully — a blocked pop-up is a refusal, not a success. */
  const openNow = useCallback(
    (cid: number, url: string): boolean => {
      if (!openLink(url)) {
        reply(cid, { ok: false, error: OPEN_LINK_BLOCKED_ERROR });
        return false;
      }
      reply(cid, { ok: true });
      return true;
    },
    [openLink, reply],
  );

  /** A frame's openLink(url) proposal, correlated by `cid`: the https + origin-allowlist gate,
   *  the open/proposal rate windows, then the chip — unless the operator trusts this source's
   *  LINKS (a separate grant from sends). */
  const proposeOpen = useCallback(
    async (cid: number, url: string) => {
      let allowOrigins: readonly string[];
      try {
        allowOrigins = await loadAllowOrigins();
      } catch {
        reply(cid, { ok: false, error: OPEN_LINK_SETTINGS_ERROR });
        showRejection(OPEN_LINK_SETTINGS_ERROR);
        return;
      }
      const verdict = bridge.checkOpenLinkProposal({ frameId, trustKey, url, allowOrigins });
      if (verdict.status === "rejected") {
        reply(cid, { ok: false, error: verdict.message });
        showRejection(verdict.message);
        return;
      }
      if (verdict.status === "ok") {
        if (!openNow(cid, verdict.url)) showRejection(OPEN_LINK_BLOCKED_ERROR);
        else if (!openChip()) setNotice(null);
        return;
      }
      const prev = openChip();
      if (prev) reply(prev.cid, { ok: false, error: supersededError(prev.action) });
      setNotice({ kind: "confirm", action: "open", cid, text: verdict.url });
    },
    [bridge, frameId, trustKey, reply, setNotice, showRejection, openNow, loadAllowOrigins],
  );

  /** The operator's answer on the chip. Send re-checks busy (the chat may have started a turn
   *  since the chip opened), records "Always allow" if ticked, then posts exactly once. Open
   *  opens the tab from THIS click (a real gesture) and reports whether it actually opened. */
  const resolve = useCallback(
    (accept: boolean, alwaysAllow: boolean, target: ProposalTarget | null) => {
      const chip = openChip();
      if (!chip) return;
      if (chip.action === "open") {
        setNotice(null); // cleared FIRST so a double click finds no chip and can't open twice
        if (!accept) {
          reply(chip.cid, { ok: false, error: OPEN_LINK_DISMISSED_ERROR });
          return;
        }
        if (!openNow(chip.cid, chip.text)) {
          setNotice({ kind: "rejected", message: OPEN_LINK_BLOCKED_ERROR });
          return;
        }
        if (alwaysAllow) bridge.trustLink(trustKey);
        bridge.noteOpen(frameId);
        return;
      }
      if (!accept) {
        setNotice(null);
        reply(chip.cid, { ok: false, error: SEND_DISMISSED_ERROR });
        return;
      }
      if (!target) {
        const message = "This frame can't send to chat here.";
        setNotice({ kind: "rejected", message });
        reply(chip.cid, { ok: false, error: message });
        return;
      }
      if (target.isBusy()) {
        const message = "the agent is busy";
        setNotice({ kind: "rejected", message });
        reply(chip.cid, { ok: false, error: message });
        return;
      }
      setNotice(null); // cleared FIRST so a double click finds no chip and can't post twice
      if (alwaysAllow) bridge.trust(trustKey);
      bridge.noteSend(frameId);
      target.post(chip.text);
      reply(chip.cid, { ok: true, text: chip.text });
    },
    [bridge, frameId, trustKey, reply, setNotice, openNow],
  );

  const clear = useCallback(() => setNotice(null), [setNotice]);

  return { notice, propose, proposeOpen, resolve, showRejection, clear };
}

/**
 * The inline notice under a frame. `classPrefix` keeps each host's existing styling
 * (`artifact-ref-inline__bridge` / `frame-component-host__bridge`); `testIdPrefix` namespaces the
 * test ids (`artifact` / `component`). `source` names what is asking ("artifact" / "component")
 * in the trust checkbox label.
 */
export function BridgeNoticeView({
  notice,
  classPrefix,
  testIdPrefix,
  source,
  onAccept,
  onDismiss,
  onClearRejection,
}: {
  notice: BridgeNotice | null;
  classPrefix: string;
  testIdPrefix: string;
  source: string;
  /** The chip's Send / Open button (with the "Always allow" box's state). */
  onAccept: (alwaysAllow: boolean) => void;
  onDismiss: () => void;
  onClearRejection: () => void;
}) {
  if (!notice) return null;
  if (notice.kind === "rejected") {
    return (
      <div className={`${classPrefix} ${classPrefix}--rejected`} data-testid={`${testIdPrefix}-send-rejected`} role="status">
        <span className={`${classPrefix}-msg`}>{notice.message}</span>
        <button
          type="button"
          className={`${classPrefix}-btn`}
          data-testid={`${testIdPrefix}-send-rejected-dismiss`}
          aria-label="Dismiss"
          onClick={onClearRejection}
        >
          Dismiss
        </button>
      </div>
    );
  }
  // Keyed by the proposal id: every new proposal gets a fresh, UNTICKED "Always allow" box, so
  // trust ticked for one message never carries over to the next one the frame proposes.
  return (
    <ConfirmChip
      key={notice.cid}
      action={notice.action}
      text={notice.text}
      classPrefix={classPrefix}
      testIdPrefix={testIdPrefix}
      source={source}
      onAccept={onAccept}
      onDismiss={onDismiss}
    />
  );
}

/** The host a link chip names up front — what the operator is really agreeing to visit. */
function linkHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function ConfirmChip({
  action,
  text,
  classPrefix,
  testIdPrefix,
  source,
  onAccept,
  onDismiss,
}: {
  action: ProposalAction;
  text: string;
  classPrefix: string;
  testIdPrefix: string;
  source: string;
  onAccept: (alwaysAllow: boolean) => void;
  onDismiss: () => void;
}) {
  const [alwaysAllow, setAlwaysAllow] = useState(false);
  const labelId = useId();
  const textId = useId();
  const open = action === "open";
  // Test ids: `<prefix>-send-confirm*` for a message, `<prefix>-open-confirm*` for a link.
  const tid = `${testIdPrefix}-${open ? "open" : "send"}-confirm`;
  // Not modal and it does NOT steal focus (the operator may be typing in the composer, and a
  // frame must not be able to yank focus by proposing) — alertdialog makes assistive tech
  // announce it; the buttons and checkbox are ordinary focusable controls.
  return (
    <div
      className={`${classPrefix} ${classPrefix}--confirm`}
      data-testid={tid}
      role="alertdialog"
      aria-modal="false"
      aria-labelledby={labelId}
      aria-describedby={textId}
    >
      {open ? (
        // The host is the decision ("Open docs.example.com in a new tab?"); the full URL follows
        // so nothing about where it goes is hidden.
        <span className={`${classPrefix}-msg`}>
          <span id={labelId}>
            Open <strong data-testid={`${tid}-host`}>{linkHost(text)}</strong> in a new tab?{" "}
          </span>
          <q id={textId} className={`${classPrefix}-text`} data-testid={`${tid}-text`}>
            {text}
          </q>
        </span>
      ) : (
        <span className={`${classPrefix}-msg`}>
          <span id={labelId}>Send to chat: </span>
          <q id={textId} className={`${classPrefix}-text`} data-testid={`${tid}-text`}>
            {text}
          </q>
        </span>
      )}
      <button
        type="button"
        className={`${classPrefix}-btn`}
        data-testid={`${tid}-ok`}
        onClick={() => onAccept(alwaysAllow)}
      >
        {open ? "Open" : "Send"}
      </button>
      <button type="button" className={`${classPrefix}-btn`} data-testid={`${tid}-dismiss`} onClick={onDismiss}>
        Dismiss
      </button>
      <label className={`${classPrefix}-trust`}>
        <input
          type="checkbox"
          data-testid={`${tid}-trust`}
          checked={alwaysAllow}
          onChange={(e) => setAlwaysAllow(e.currentTarget.checked)}
        />
        {open ? `Always allow links from this ${source}` : `Always allow from this ${source}`}
      </label>
    </div>
  );
}
