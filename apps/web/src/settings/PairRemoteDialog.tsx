import { useMutation } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { Alert } from "@protolabsai/ui/data";
import { Checkbox, Input, Switch } from "@protolabsai/ui/forms";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";

import {
  formatAgentCode,
  INSECURE_OPT_IN,
  INSECURE_WARNING,
  isCompleteAgentCode,
  isInsecureRefusal,
  needsInsecureOptIn,
} from "../lib/agentPairing";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";

/** What the dialog is pairing with. `repair` keeps the URL fixed: the hub re-tokens the
 *  member registered at that exact URL, so editing it would silently add a second member. */
export type PairTarget = {
  mode: "pair" | "repair";
  url: string;
  /** Display name for the title (the discovered card name, or the member's label). */
  label: string;
  /** The name the server will default to (a discovered agent's card name) — shown as the
   *  placeholder, NOT prefilled: an explicit name must pass the member charset, while the
   *  server slugifies a defaulted card name ("Ava Agent" → a valid handle) on its own. */
  name?: string;
};

export type PairResult = Awaited<ReturnType<typeof api.pairRemote>>;

/**
 * Pair… / Re-pair (ADR 0113 D1) — type the code the remote showed, and the HUB's server
 * redeems it. The token it gets back goes straight into `remotes.json`; this browser never
 * sees it, which is the point: nothing is copied by hand, and the remote can revoke this hub
 * on its own.
 *
 * Errors are the server's `detail` VERBATIM ("that code is invalid or expired — generate a
 * new one on the remote", "… is unreachable …"): the server already words them for the
 * operator, and paraphrasing here would lose the URL/status it names.
 *
 * `offerDelegate` adds "Also add as a delegate of this agent" — the pair-then-link path that
 * replaced a discovered row's tokenless `<url>/a2a` delegate (see `delegateLinkFor`). It is
 * only offered where the resulting hub-proxy URL is valid for the focused agent.
 */
export function PairRemoteDialog({
  target,
  offerDelegate,
  onClose,
  onPaired,
}: {
  target: PairTarget | null;
  offerDelegate: boolean;
  onClose: () => void;
  onPaired: (res: PairResult, opts: { addDelegate: boolean }) => void;
}) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [alsoDelegate, setAlsoDelegate] = useState(false);
  // ADR 0113 D10 — plain http to a non-loopback, non-tailnet host needs an explicit opt-in.
  // `insecureRevealed` is the hub's own 400 for a case this browser couldn't classify (an
  // http NAME that resolved to a LAN address): the opt-in appears after the refusal.
  const [allowInsecure, setAllowInsecure] = useState(false);
  const [insecureRevealed, setInsecureRevealed] = useState(false);
  const hintId = useId();
  // The DS Input doesn't forward a ref, so the code field is found by its (unique) id.
  const codeId = useId();
  const urlId = useId();

  // Reset per open — a stale code from the last attempt is spent (single-use) or wrong.
  useEffect(() => {
    if (!target) return;
    setUrl(target.url);
    setCode("");
    setName("");
    setAlsoDelegate(offerDelegate);
    setAllowInsecure(false);
    setInsecureRevealed(false);
    // Land in the CODE field — the one thing the operator came to type. The DS focus trap
    // focuses the dialog's first focusable (the close button) on open, so `autoFocus` alone
    // loses; this runs a frame later and wins. For "Pair by URL…" the URL is still empty, so
    // start there instead.
    const id = requestAnimationFrame(() => {
      document.getElementById(target.url ? codeId : urlId)?.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [target, offerDelegate, codeId, urlId]);

  const askInsecure = needsInsecureOptIn(url) || insecureRevealed;
  const pair = useMutation({
    mutationFn: () =>
      api.pairRemote({
        url: url.trim(),
        code: formatAgentCode(code),
        ...(name.trim() ? { name: name.trim() } : {}),
        // Only ever sent as the operator's ticked answer to the warning — never by default.
        ...(askInsecure && allowInsecure ? { allow_insecure: true } : {}),
      }),
    onSuccess: (res) => {
      const who = res.agent?.label ?? res.agent?.name ?? target?.label ?? "the remote";
      if (res.auth === "rejected") {
        // Claimed, but the authenticated probe was refused right after — say so rather than
        // celebrating a member whose first click will 401.
        toast({ tone: "warning", title: `Paired ${who}`, message: "…but it refused the new token. Try re-pairing." });
      } else if (res.reachable === false) {
        toast({ tone: "warning", title: `Paired ${who}`, message: "Paired, but it isn't reachable right now." });
      } else {
        toast({
          tone: "success",
          title: res.action === "retokened" ? `Re-paired ${who}` : `Paired ${who}`,
          message: res.action === "retokened" ? "Its new token is stored on this hub." : `${who} joined the fleet.`,
        });
      }
      onPaired(res, { addDelegate: offerDelegate && alsoDelegate });
      onClose();
    },
    onError: (e) => {
      if (isInsecureRefusal(e)) setInsecureRevealed(true);
      toast({ tone: "error", title: "Couldn't pair", message: errMsg(e) });
    },
  });

  const urlOk = /^https?:\/\/.+/.test(url.trim()); // same gate as the add-by-URL form
  const ready = urlOk && isCompleteAgentCode(code) && !pair.isPending && (!askInsecure || allowInsecure);
  const title = target ? (target.mode === "repair" ? `Re-pair ${target.label}` : `Pair with ${target.label}`) : "";

  return (
    <Dialog
      open={target !== null}
      onClose={pair.isPending ? undefined : onClose}
      title={title}
      width={460}
      className="fleet-pair-dialog"
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onClose} disabled={pair.isPending}>
            Cancel
          </Button>
          <Button type="submit" form="fleet-pair-form" variant="primary" disabled={!ready} loading={pair.isPending}>
            {pair.isPending ? "Pairing…" : target?.mode === "repair" ? "Re-pair" : "Pair"}
          </Button>
        </>
      }
    >
      <form
        id="fleet-pair-form"
        className="fleet-pair-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) pair.mutate();
        }}
      >
        <p className="setting-desc" id={hintId}>
          On {target?.mode === "repair" ? target.label : (target?.name ?? "the other agent")}, open <strong>Settings ▸ Devices ▸ Pair an agent</strong>{" "}
          (or run <code>protoagent pair</code> there) and type the code it shows. It expires in 5 minutes.
        </p>
        <label className="field">
          <span>URL</span>
          <Input
            id={urlId}
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              // A new URL is a new question — don't carry a consent given for another host.
              setAllowInsecure(false);
              setInsecureRevealed(false);
            }}
            readOnly={target?.mode === "repair"}
            aria-readonly={target?.mode === "repair" || undefined}
            placeholder="http://100.x.y.z:7870"
            inputMode="url"
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        {askInsecure ? (
          <Alert status="warning" className="fleet-insecure">
            <p>{INSECURE_WARNING}</p>
            <Checkbox
              checked={allowInsecure}
              onCheckedChange={setAllowInsecure}
              required
              label={INSECURE_OPT_IN}
              data-testid="fleet-pair-insecure"
            />
          </Alert>
        ) : null}
        <label className="field">
          <span>Code</span>
          <Input
            className="fleet-pair-code"
            value={code}
            // Formatted AS TYPED: upper-cased, separators tolerated, the dash re-inserted after
            // five — so a pasted `abcde fghij` reads back exactly like the remote's screen.
            onChange={(e) => setCode(formatAgentCode(e.target.value))}
            id={codeId}
            placeholder="XXXXX-XXXXX"
            aria-describedby={hintId}
            autoComplete="one-time-code"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            maxLength={16}
            data-testid="fleet-pair-code"
          />
        </label>
        <label className="field">
          <span>Name (optional)</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={
              target?.mode === "repair"
                ? "leave blank to keep the current name"
                : target?.name
                  ? `defaults to ${target.name}`
                  : "defaults to the remote's own name"
            }
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        {offerDelegate ? (
          <Switch
            checked={alsoDelegate}
            onCheckedChange={setAlsoDelegate}
            label="Also add it as a delegate of this agent"
          />
        ) : null}
      </form>
    </Dialog>
  );
}
