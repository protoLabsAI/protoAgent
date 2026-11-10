import { Fragment, useLayoutEffect, useRef, useState } from "react";
import { Drawer } from "@protolabsai/ui/overlays";
import { Accordion, AccordionItem } from "@protolabsai/ui/navigation";
import { Button, Callout, Count, Divider, Empty } from "@protolabsai/ui/primitives";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { useIsMobile } from "../lib/useIsMobile";
import type { DirectiveRow } from "../lib/api/chat";
import { DirectiveEntry } from "./DirectiveEntry";
import { useDirectives } from "./useDirectives";

export function DirectiveDrawer({ sessionId, open, onClose }: { sessionId: string; open: boolean; onClose: () => void }) {
  const opener = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      const target = opener.current;
      if (target?.isConnected && target !== document.body) target.focus();
      else {
        // The current console uses PromptInput inside composer-wrap; skip cached, hidden tabs.
        const composer = document.querySelector<HTMLTextAreaElement>(".chat-composer textarea")
          ?? document.querySelector<HTMLTextAreaElement>(".chat-session-slot:not([hidden]) .composer-wrap textarea");
        composer?.focus();
      }
    };
  }, [open]);
  return <Drawer open={open} onClose={onClose} side={useIsMobile() ? "bottom" : "right"} title="Standing instructions">
    {open && <DirectiveDrawerBody key={sessionId} sessionId={sessionId} />}
  </Drawer>;
}

export function DirectiveDrawerBody({ sessionId }: { sessionId: string }) {
  const { state, active, history, loadedAt, refresh, revoke } = useDirectives(sessionId);
  // A refresh can remove a row before revoke resolves. Keep its component alive so
  // the result callout remains in its place for this view, including keyboard focus.
  const [retained, setRetained] = useState<DirectiveRow[]>([]);
  const [focusAfter, setFocusAfter] = useState<string[] | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const quotes = useRef(new Map<string, HTMLDivElement>());
  const visible = [...active.filter((d) => !retained.some((r) => r.id === d.id)), ...retained]
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  useLayoutEffect(() => {
    if (!focusAfter) return;
    const next = focusAfter.map((id) => quotes.current.get(id)?.querySelector<HTMLElement>("blockquote")).find(Boolean);
    (next ?? heading.current)?.focus();
    setFocusAfter(null);
  }, [focusAfter]);
  const retry = <Button variant="ghost" size="sm" onClick={() => void refresh()}>Retry</Button>;
  return <div>
    <p>What this agent believes you've allowed it to do in this chat. It re-reads everything under "Active" on every turn.</p>
    {state === "stale" && <Callout tone="neutral">Showing what was loaded at {new Date(loadedAt!).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })}. Couldn't refresh. {retry}</Callout>}
    {state !== "failed" && <h3 ref={heading} tabIndex={-1}>Active (<Count>{active.length}</Count>)</h3>}
    {state === "loading" ? <><Skeleton lines={3} /><Skeleton lines={3} /></> : state === "failed" ?
      <Alert status="warning" title="Couldn't load standing instructions" action={retry}>The agent still has them. Only this view failed to load.</Alert> : <>
        {!visible.length && !history.length && <Empty title="No standing instructions" description="When you give this agent an instruction or approval that lasts beyond one reply, it records your words here. You can withdraw any of them at any time." />}
        {!visible.length && history.length > 0 && <p>Nothing is active right now.</p>}
        {visible.map((entry, index) => <Fragment key={entry.id}>
          {index > 0 && <Divider />}
          <div ref={(node) => { if (node) quotes.current.set(entry.id, node); else quotes.current.delete(entry.id); }}>
            <DirectiveEntry entry={entry} revoke={async (id, reason) => {
              setRetained((rows) => [...rows.filter((r) => r.id !== id), entry]);
              const result = await revoke(id, reason);
              if ("ok" in result || "error" in result) setRetained((rows) => rows.filter((r) => r.id !== id));
              if ("ok" in result) setFocusAfter([...visible.slice(index + 1), ...visible.slice(0, index)].map((d) => d.id));
              return result;
            }} />
          </div>
        </Fragment>)}
        {history.length > 0 && <Accordion><AccordionItem title={`History (${history.length})`}>
          {[...history].sort((a, b) => Date.parse(b.closed_at ?? b.created_at) - Date.parse(a.closed_at ?? a.created_at)).map((entry, index) => <Fragment key={entry.id}>
            {index > 0 && <Divider />}
            <DirectiveEntry entry={entry} revoke={revoke} />
          </Fragment>)}
        </AccordionItem></Accordion>}
      </>}
  </div>;
}
