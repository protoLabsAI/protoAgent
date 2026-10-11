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
// The hosts own the message plumbing (e.source checks, origin tags, replying to their frame);
// this owns the decision state and the chip UI.

import { useCallback, useId, useRef, useState } from "react";

import type { FrameBridge } from "./frameBridge";

/** What the host shows under its frame: the confirm chip, or a rejection the operator sees in
 *  place ("the agent is busy", a refused link). */
export type BridgeNotice = { kind: "confirm"; cid: number; text: string } | { kind: "rejected"; message: string };

export type FrameReply = (cid: number, result: { ok: boolean; text?: string; error?: string }) => void;

/** The chat a proposal posts into: `isBusy` for the gate, `post` runs the NORMAL origin-tagged
 *  send path (the host closes over its own origin tag). */
export type ProposalTarget = { isBusy: () => boolean; post: (text: string) => void };

/** Error strings the frame's send() Promise rejects with. */
export const SEND_DISMISSED_ERROR = "The user dismissed this message — it was not sent to chat.";
export const SEND_SUPERSEDED_ERROR = "Replaced by a newer message from this frame before the user answered.";

export function useFrameSendGate({
  bridge,
  frameId,
  trustKey,
  reply,
}: {
  bridge: FrameBridge;
  frameId: string;
  trustKey: string;
  reply: FrameReply;
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
      if (prev) reply(prev.cid, { ok: false, error: SEND_SUPERSEDED_ERROR });
      setNotice({ kind: "confirm", cid, text: verdict.text });
    },
    [bridge, frameId, trustKey, reply, setNotice, showRejection],
  );

  /** The operator's answer on the chip. Send re-checks busy (the chat may have started a turn
   *  since the chip opened), records "Always allow" if ticked, then posts exactly once. */
  const resolve = useCallback(
    (accept: boolean, alwaysAllow: boolean, target: ProposalTarget | null) => {
      const chip = openChip();
      if (!chip) return;
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
    [bridge, frameId, trustKey, reply, setNotice],
  );

  const clear = useCallback(() => setNotice(null), [setNotice]);

  return { notice, propose, resolve, showRejection, clear };
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
  onSend,
  onDismiss,
  onClearRejection,
}: {
  notice: BridgeNotice | null;
  classPrefix: string;
  testIdPrefix: string;
  source: string;
  onSend: (alwaysAllow: boolean) => void;
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
      text={notice.text}
      classPrefix={classPrefix}
      testIdPrefix={testIdPrefix}
      source={source}
      onSend={onSend}
      onDismiss={onDismiss}
    />
  );
}

function ConfirmChip({
  text,
  classPrefix,
  testIdPrefix,
  source,
  onSend,
  onDismiss,
}: {
  text: string;
  classPrefix: string;
  testIdPrefix: string;
  source: string;
  onSend: (alwaysAllow: boolean) => void;
  onDismiss: () => void;
}) {
  const [alwaysAllow, setAlwaysAllow] = useState(false);
  const labelId = useId();
  const textId = useId();
  // Not modal and it does NOT steal focus (the operator may be typing in the composer, and a
  // frame must not be able to yank focus by proposing) — alertdialog makes assistive tech
  // announce it; the buttons and checkbox are ordinary focusable controls.
  return (
    <div
      className={`${classPrefix} ${classPrefix}--confirm`}
      data-testid={`${testIdPrefix}-send-confirm`}
      role="alertdialog"
      aria-modal="false"
      aria-labelledby={labelId}
      aria-describedby={textId}
    >
      <span className={`${classPrefix}-msg`}>
        <span id={labelId}>Send to chat: </span>
        <q id={textId} className={`${classPrefix}-text`} data-testid={`${testIdPrefix}-send-confirm-text`}>
          {text}
        </q>
      </span>
      <button
        type="button"
        className={`${classPrefix}-btn`}
        data-testid={`${testIdPrefix}-send-confirm-ok`}
        onClick={() => onSend(alwaysAllow)}
      >
        Send
      </button>
      <button
        type="button"
        className={`${classPrefix}-btn`}
        data-testid={`${testIdPrefix}-send-confirm-dismiss`}
        onClick={onDismiss}
      >
        Dismiss
      </button>
      <label className={`${classPrefix}-trust`}>
        <input
          type="checkbox"
          data-testid={`${testIdPrefix}-send-confirm-trust`}
          checked={alwaysAllow}
          onChange={(e) => setAlwaysAllow(e.currentTarget.checked)}
        />
        Always allow from this {source}
      </label>
    </div>
  );
}
