import "../watches/watches.css";

import { Button, Empty } from "@protolabsai/ui/primitives";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import {
  QueryErrorResetBoundary,
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { Suspense, useEffect, useState } from "react";

import { api } from "../lib/api";
import { HitlForm } from "../chat/HitlForm";
import { buildWatchCreateBody, watchFormPayload, type WatchCreateBody } from "../chat/watchForm";
import { errMsg } from "../lib/format";
import { onServerEvent } from "../lib/events";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { watchesQuery, verifiersQuery, queryKeys } from "../lib/queries";
import { watchLifetime } from "./workOverview";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { ScrollArea } from "@protolabsai/ui/data";
import { StatusPill } from "./StatusPill";

// The agent's watches (ADR 0067 passive-supervision layer), in the Work hub. Mirrors
// GoalsPanel on the TanStack Query + Suspense + ErrorBoundary data layer (ADR 0013): the read
// is a `useSuspenseQuery` (loading → <Suspense>, failure → <ErrorBoundary>), and clearing a
// watch is a `useMutation` that invalidates the watches query. Unlike goals (one per session,
// keyed by session_id), a watch is verifier-only and keyed by its own `id` — many at once.

function watchTone(status: string) {
  if (status === "met") return "success" as const;
  if (status === "active") return "warning" as const;
  if (status === "expired") return "error" as const;
  return "muted" as const;
}

const trunc = (t: string, n = 80) => (t.length > n ? `${t.slice(0, n)}…` : t);

function WatchesList() {
  const { data } = useSuspenseQuery(watchesQuery());
  const watches = data.watches;
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: (id: string) => api.clearWatch(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.watches }),
  });

  // Live: refresh off the watch bus instead of polling, the same pattern as GoalsPanel.
  // `watch.changed` fires on create/check/clear; `watch.met`/`watch.expired`/`watch.stalled`
  // fire on the terminal/stall transitions — any of them re-reads the list.
  useEffect(() => {
    const refresh = () => void queryClient.invalidateQueries({ queryKey: queryKeys.watches });
    const offs = [
      onServerEvent("watch.changed", refresh),
      onServerEvent("watch.met", refresh),
      onServerEvent("watch.expired", refresh),
      onServerEvent("watch.stalled", refresh),
    ];
    return () => offs.forEach((off) => off());
  }, [queryClient]);

  if (!watches.length) {
    return (
      <Empty
        title="No watches"
        description={
          <>
            set one with <code>New watch</code> — or the agent will, when you ask it to keep an
            eye on something
          </>
        }
      />
    );
  }

  return (
    <>
      {watches.map((watch) => (
        <div className="watch-row" key={watch.id}>
          <div className="watch-row-head">
            <strong>{watch.condition || watch.id}</strong>
            <StatusPill label={watch.status} tone={watchTone(watch.status)} />
          </div>
          {/* Facts are separate elements, not one interpolated string, so the ` · `
              separators are structural (a CSS ::after) and each fact stays whole when the
              line wraps — an embedded separator put a dangling "· " at the start of a
              wrapped line. Lifetime knobs are ADR 0067 / #2325: the agent has been able to
              SET these and reads them back from `list_watches`, so without them here the
              operator was the one party who couldn't see when a watch expires. */}
          <span className="watch-row-meta">
            <span className="watch-row-fact">{watch.id}</span>
            <span className="watch-row-fact">{watch.verifier?.type || "llm"}</span>
            {watchLifetime(watch).map((part) => (
              <span
                key={part}
                className={
                  part === "past its deadline" ? "watch-row-fact watch-row-fact-warn" : "watch-row-fact"
                }
              >
                {part}
              </span>
            ))}
            {watch.last_reason ? (
              <span className="watch-row-fact watch-row-reason">{trunc(watch.last_reason)}</span>
            ) : null}
          </span>
          <Button
            icon variant="ghost" className="watch-row-clear"
            type="button"
            onClick={() => clear.mutate(watch.id)}
            disabled={clear.isPending}
            title="Clear watch"
          >
            <Trash2 size={15} />
          </Button>
        </div>
      ))}
    </>
  );
}

// Operator "new watch" dialog — ONE creator, two hosts (mirroring GoalCreateDialog): the
// Watches panel's header action and the Work overview's Watches-card quick-add both open it.
// Chromeless DS Dialog because `HitlForm` brings its own title, stepper and actions.
//
// The answers map through the shared `buildWatchCreateBody` to `POST /api/watches` — the
// TRUSTED channel (ADR 0066 path ceiling), so this form can arm the command/test/ci/data
// verifiers the agent's own `create_watch` is denied. Until this existed the console could
// only CLEAR a watch, which meant the console exposed strictly less than the API it fronts.
export function WatchCreateDialog({
  open,
  onClose,
  onCreate,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (body: WatchCreateBody) => void;
  busy: boolean;
}) {
  // The verifier options come from the server (core types + whatever plugins registered),
  // not a hardcoded list. `enabled: open` so it's fetched when the dialog is first opened
  // rather than on every panel mount; `data` undefined (still loading, or the fetch failed)
  // falls through to `watchFormPayload`'s core-type default.
  const { data: catalog } = useQuery({ ...verifiersQuery(), enabled: open });
  return (
    <Dialog open={open} onClose={onClose} width="min(560px, 94vw)" className="watch-create-modal">
      <div data-testid="watch-create-dialog">
        <HitlForm
          payload={watchFormPayload(catalog)}
          busy={busy}
          onSubmit={(answers) => {
            const body = buildWatchCreateBody(
              typeof answers === "object" && answers ? (answers as Record<string, unknown>) : {},
            );
            if (body) onCreate(body);
          }}
          onCancel={onClose}
        />
      </div>
    </Dialog>
  );
}

export function WatchesPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const create = useMutation({
    mutationFn: (body: WatchCreateBody) => api.createWatch(body),
    onSuccess: (res) => {
      setCreating(false);
      toast({ tone: "success", title: "Watch set", message: res.message || "The agent is watching for it." });
    },
    // A rejected verifier comes back HTTP 400 → request() throws here.
    onError: (e) => toast({ tone: "error", title: "Couldn't set watch", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.watches }),
  });
  return (
    <section className="panel side-panel watches-panel">
      <PanelHeader
        compact
        title="Watches"
        kicker={<>conditions something else moves · polled out-of-band</>}
        actions={
          <Button
            variant="primary"
            type="button"
            onClick={() => {
              create.reset();
              setCreating(true);
            }}
            data-testid="watch-new"
          >
            <Plus size={16} /> New watch
          </Button>
        }
      />
      <ScrollArea className="watches-list" role="region" aria-label="Watches" tabIndex={0}>
        <QueryErrorResetBoundary>
          {({ reset }: { reset: () => void }) => (
            <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="watches" />}>
              <Suspense fallback={<PanelSkeleton label="Loading watches…" />}>
                <WatchesList />
              </Suspense>
            </ErrorBoundary>
          )}
        </QueryErrorResetBoundary>
      </ScrollArea>
      <WatchCreateDialog
        open={creating}
        onClose={() => {
          setCreating(false);
          create.reset();
        }}
        onCreate={(body) => create.mutate(body)}
        busy={create.isPending}
      />
    </section>
  );
}
