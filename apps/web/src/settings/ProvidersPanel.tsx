import "./settings.css";
import "./providers.css";

import { DropdownSelect, Input, SecretInput } from "@protolabsai/ui/forms";
import type { DropdownSelectOption } from "@protolabsai/ui/forms";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { ConfirmDialog, Dialog, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { bareModel, groupByLane, laneLabel, lanesFromOptions, modelPickerData } from "../chat/modelForm";
import { errMsg } from "../lib/format";
import { settingsSchemaQuery } from "../lib/queries";
import { OAUTH_PROVIDER_LABEL, OAuthAccountCard } from "../oauth/OAuthAccount";

// Settings ▸ Model ▸ Connections (ADR 0106). Replaces the account section that could
// only ever describe ONE provider as real and every other as "connected but isn't the
// current default" — a sentence that only made sense because config had a single lead
// provider. There is no active/default state here: a connection either works or it
// doesn't, and model pickers choose among all of them.
//
// A list of objects, so it gets a panel rather than fields in the generic settings
// schema (which is a flat map of dotted keys with a scalar type each).

const TYPE_LABEL: Record<string, string> = {
  "openai-compat": "OpenAI-compatible endpoint",
  "anthropic-oauth": "Claude subscription",
  "openai-codex": "ChatGPT / Codex subscription",
};

export const CONNECTION_TYPE_OPTIONS = Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label }));

type ProviderDraft = {
  id: string;
  type: string;
  label: string;
  base_url: string;
  api_key: string;
};

type ProviderView = Awaited<ReturnType<typeof api.providers>>["providers"][number];

// OAuth connections use the established lane ids by default. They remain editable in
// the form, but the useful default preserves the qualified names operators already know
// (`anthropic-oauth:…` / `openai-codex:…`) and gets them to sign-in in one click.
export function providerDraftForType(type = "openai-compat"): ProviderDraft {
  if (type === "anthropic-oauth") {
    return { id: "anthropic-oauth", type, label: "Claude", base_url: "", api_key: "" };
  }
  if (type === "openai-codex") {
    return { id: "openai-codex", type, label: "ChatGPT / Codex", base_url: "", api_key: "" };
  }
  return { id: "", type: "openai-compat", label: "", base_url: "", api_key: "" };
}

// Mirrors `valid_provider_id` in graph/config.py — the console refuses locally what the
// route would refuse anyway, so a typo is caught before a round trip. Exported so the
// rule is testable on its own; the two must not drift, since an id the backend rejects
// would otherwise only surface as a 400.
export const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

export function providerIdError(id: string): string {
  if (!id) return "";
  return ID_RE.test(id)
    ? ""
    : "Lowercase letters, digits, - and _ only — ':' and '/' are reserved by the provider:model grammar.";
}

// ── Resolve-references dialog (bd-v6xy) ────────────────────────────────────────
// Removing a connection a model slot names is REFUSED (409) by the route, on purpose:
// a silent removal would leave the slot resolving to a bare id against whatever remains.
// This dialog offers the resolution IN the panel — clear or repoint each blocking
// reference, then remove in one request — instead of leaving the operator to hunt each
// slot down elsewhere in Settings.

/** One structured reference the route reported for a connection (`in_use[]`). */
export type InUseEntry = NonNullable<ProviderView["in_use"]>[number];

/** The dropdown value that means "clear this slot" (release → null). A sentinel, not a
 *  model, so it can never collide with a real `<pid>:<model>` lane option. */
export const CLEAR_TARGET = "__clear__";

/** A per-reference selection map: reference key → the chosen dropdown value (a qualified
 *  `<pid>:<model>` lane, or CLEAR_TARGET). Favorites carry no selection — always cleared. */
export type ReferenceSelections = Record<string, string>;

// Friendly labels for the fixed slots; anything else (a subagent) is humanised below.
const SLOT_LABELS: Record<string, string> = {
  "model.name": "Lead model",
  "routing.aux_model": "Auxiliary model",
  "compaction.model": "Compaction model",
  "goal.eval_model": "Goal-eval model",
  "soul.drift_judge_model": "Drift-judge model",
  "model.favorites": "Favorite models",
};

/** Humanise a dotted reference key for the row label — subagents read as
 *  "Subagent <name> model", the fixed slots get their friendly names, and anything
 *  unknown degrades to a readable spacing of its dotted key. */
export function humaniseReferenceKey(key: string): string {
  if (key.startsWith("subagents.") && key.endsWith(".model")) {
    return `Subagent ${key.slice("subagents.".length, -".model".length)} model`;
  }
  return SLOT_LABELS[key] ?? key.replace(/_/g, " ").replace(/\./g, " · ");
}

/** Initial selections: every clearable slot defaults to Clear; `model.name` (not
 *  clearable) and favorites get no default — the operator must choose a repoint for the
 *  former, and the latter is always cleared. */
export function defaultReferenceSelections(inUse: InUseEntry[]): ReferenceSelections {
  const out: ReferenceSelections = {};
  for (const e of inUse) if (e.kind !== "favorite" && e.clearable) out[e.key] = CLEAR_TARGET;
  return out;
}

/** True once every reference that MUST be repointed (the non-clearable `model.name`) has a
 *  concrete lane chosen — the gate on the dialog's primary button. Clearable slots and
 *  favorites need nothing (they clear), so they never block. */
export function referencesResolved(inUse: InUseEntry[], selections: ReferenceSelections): boolean {
  return inUse
    .filter((e) => e.kind !== "favorite" && !e.clearable)
    .every((e) => Boolean(selections[e.key]) && selections[e.key] !== CLEAR_TARGET);
}

/** The repoint options for the dialog: every lane's qualified `<pid>:<model>` models
 *  EXCEPT the connection being removed (its models are about to disappear), grouped by
 *  connection. `qualified` is the settings schema's cross-provider option list — the same
 *  lanes source the settings model pickers use, so no new probe is issued. */
export function otherConnectionGroups(qualified: string[], removingPid: string) {
  return groupByLane(qualified).filter((g) => g.lane && g.lane !== removingPid);
}

/** Build the DELETE `releases` map from the operator's selections: Clear (or a clearable
 *  slot left at its default) → null; a favorites entry → null; a chosen lane → its
 *  qualified `<pid>:<model>` value. A non-clearable reference with nothing chosen is
 *  omitted (the primary button is disabled until it is resolved, so this is unreachable
 *  in practice — but omitting beats emitting a clear the backend would reject). */
export function buildReleases(inUse: InUseEntry[], selections: ReferenceSelections): Record<string, string | null> {
  const releases: Record<string, string | null> = {};
  for (const e of inUse) {
    if (e.kind === "favorite") {
      releases[e.key] = null;
      continue;
    }
    const sel = selections[e.key];
    if (e.clearable && (!sel || sel === CLEAR_TARGET)) releases[e.key] = null;
    else if (sel && sel !== CLEAR_TARGET) releases[e.key] = sel;
  }
  return releases;
}

export function ProvidersPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["providers"], queryFn: () => api.providers() });
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [confirmLast, setConfirmLast] = useState<string | null>(null);
  const [formTarget, setFormTarget] = useState<ProviderView | "add" | null>(null);
  const [resolveTarget, setResolveTarget] = useState<ProviderView | null>(null);

  const refresh = () => void qc.invalidateQueries({ queryKey: ["providers"] });

  const remove = useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: boolean }) => api.removeProvider(id, confirm),
    onSuccess: (r) => {
      toast({ tone: "success", title: "Connection removed", message: r.removed });
      refresh();
    },
    // The 409 body names the slots still routing through it — that IS the message.
    onError: (e) => toast({ tone: "error", title: "Still in use", message: errMsg(e) }),
  });

  const probe = useMutation({
    mutationFn: (id: string) => api.providerModels(id).then((r) => ({ id, ...r })),
    onSuccess: (r) => {
      if (r.error) {
        toast({ tone: "error", title: "Couldn't reach it", message: r.error });
        return;
      }
      setModels((m) => ({ ...m, [r.id]: r.models }));
      toast({
        tone: r.models.length ? "success" : "info",
        title: r.models.length ? `${r.models.length} model${r.models.length === 1 ? "" : "s"}` : "No models",
        message: r.models.length ? "Pick one in any model slot." : "The connection answered with an empty list.",
      });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't reach it", message: errMsg(e) }),
  });

  const rows = data?.providers ?? [];

  return (
    <div className="providers-panel" data-testid="providers-panel">
      <p className="muted">
        Every model source this agent can use. Model slots pick from all of them — there is no
        default to switch between.
      </p>

      {isLoading ? <p className="muted">Loading…</p> : null}

      <div className="provider-list">
        {rows.map((p) => (
          <div key={p.id} className="provider-row" data-testid="provider-row">
            <div className="provider-row__head">
              <div>
                <span className="provider-row__name">{p.display}</span>
                <code className="provider-row__id">{p.id}</code>
                <Badge>{TYPE_LABEL[p.type] ?? p.type}</Badge>
              </div>
              <div className="provider-row__actions">
                <Button size="sm" variant="ghost" onClick={() => setFormTarget(p)}>
                  Edit
                </Button>
                <Button size="sm" variant="ghost" onClick={() => probe.mutate(p.id)} disabled={probe.isPending}>
                  Test
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove ${p.display}`}
                  onClick={() => {
                    // A connection any slot still routes through can't be removed silently —
                    // resolve those references first (clear/repoint) in a dialog. Otherwise
                    // the removal is direct, or the existing last-connection confirmation.
                    if ((p.in_use ?? []).length) setResolveTarget(p);
                    else if (rows.length === 1) setConfirmLast(p.id);
                    else remove.mutate({ id: p.id, confirm: false });
                  }}
                  disabled={remove.isPending}
                >
                  <Trash2 size={14} />
                </Button>
              </div>
            </div>

            {p.base_url ? <div className="provider-row__meta">{p.base_url}</div> : null}

            {/* An OAuth connection carries its own sign-in lifecycle; an endpoint carries a key. */}
            {OAUTH_PROVIDER_LABEL[p.type] ? (
              <OAuthAccountCard provider={p.type} />
            ) : (
              <Callout tone={p.has_key ? "success" : "warning"}>
                <KeyRound size={15} />{" "}
                {p.has_key
                  ? "Connected — API key stored."
                  : "No API key — slots naming this connection will fail."}
              </Callout>
            )}

            {/* Why a delete may be refused, before it is attempted. */}
            {p.in_use_by.length ? (
              <div className="provider-row__meta provider-row__inuse">
                In use by {p.in_use_by.length} slot{p.in_use_by.length === 1 ? "" : "s"}:{" "}
                {p.in_use_by.join(", ")}
              </div>
            ) : null}

            {models[p.id]?.length ? (
              <div className="provider-row__meta">{models[p.id].slice(0, 8).join(" · ")}</div>
            ) : null}
          </div>
        ))}
      </div>

      <Button size="sm" variant="ghost" onClick={() => setFormTarget("add")} data-testid="add-provider">
        <Plus size={14} /> Add a connection
      </Button>

      {formTarget ? (
        <ProviderConnectionDialog
          key={formTarget === "add" ? "add" : formTarget.id}
          initial={formTarget === "add" ? null : formTarget}
          onClose={() => setFormTarget(null)}
          onSaved={() => {
            setFormTarget(null);
            refresh();
          }}
        />
      ) : null}

      {resolveTarget ? (
        <ResolveReferencesDialog
          key={resolveTarget.id}
          provider={resolveTarget}
          connections={rows}
          isLast={rows.length === 1}
          onClose={() => setResolveTarget(null)}
          onRemoved={(removed) => {
            // Drop the removed connection's probed model list so a re-used id never
            // inherits a stale one.
            setModels((m) => {
              const next = { ...m };
              delete next[removed];
              return next;
            });
            // Invalidate the providers list AND the settings schema: the repointed slots
            // live in the schema every model picker + chip reads, so refreshing only
            // ["providers"] (today's refresh()) would leave those showing the OLD value.
            void qc.invalidateQueries({ queryKey: ["providers"] });
            void qc.invalidateQueries({ queryKey: settingsSchemaQuery().queryKey });
            setResolveTarget(null);
          }}
        />
      ) : null}

      <ConfirmDialog
        open={confirmLast !== null}
        title="Remove the last connection?"
        confirmLabel="Remove connection"
        destructive
        onConfirm={() => {
          const id = confirmLast;
          setConfirmLast(null);
          if (id) remove.mutate({ id, confirm: true });
        }}
        onClose={() => setConfirmLast(null)}
      >
        This agent will have no configured model source. Add another connection first unless you intend to leave it
        unable to run model-backed work.
      </ConfirmDialog>
    </div>
  );
}

function ProviderConnectionDialog({
  initial,
  onClose,
  onSaved,
}: {
  initial: ProviderView | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const editing = initial !== null;
  const [draft, setDraft] = useState<ProviderDraft>(() =>
    initial
      ? {
          id: initial.id,
          type: initial.type,
          label: initial.label ?? "",
          base_url: initial.base_url ?? "",
          api_key: "",
        }
      : providerDraftForType(),
  );
  const idError = providerIdError(draft.id);

  const save = useMutation({
    mutationFn: () =>
      initial
        ? api.updateProvider(initial.id, {
            label: draft.label,
            base_url: draft.base_url,
            api_key: draft.api_key,
          })
        : api.addProvider(draft),
    onSuccess: () => {
      const oauth = Boolean(OAUTH_PROVIDER_LABEL[draft.type]);
      toast({
        tone: "success",
        title: editing ? "Connection updated" : "Connection added",
        message: editing
          ? `${draft.id} is ready.`
          : oauth
            ? `${draft.id} was added. Sign in below to finish connecting it.`
            : `${draft.id} is ready to use in model slots.`,
      });
      onSaved();
    },
    onError: (e) =>
      toast({
        tone: "error",
        title: editing ? "Couldn't update the connection" : "Couldn't add the connection",
        message: errMsg(e),
      }),
  });

  const close = () => {
    if (!save.isPending) onClose();
  };
  const valid = editing || (!!draft.id && !idError);

  return (
    <Dialog
      open
      onClose={close}
      title={editing ? `Edit ${initial.display}` : "Add a connection"}
      width="min(520px, 94vw)"
      className="provider-dialog"
      footer={
        <>
          <Button type="button" variant="ghost" onClick={close} disabled={save.isPending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            type="submit"
            form="provider-connection-form"
            loading={save.isPending}
            disabled={!valid || save.isPending}
          >
            {editing ? "Save connection" : "Add connection"}
          </Button>
        </>
      }
    >
      <form
        id="provider-connection-form"
        className="provider-dialog__form"
        data-testid={initial ? `provider-edit-${initial.id}` : "provider-add-form"}
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && !save.isPending) save.mutate();
        }}
      >
        {!editing ? (
          <label className="field">
            <span>Connection type</span>
            <DropdownSelect
              id="provider-connection-type"
              value={draft.type}
              onValueChange={(type) => setDraft(providerDraftForType(type))}
              options={CONNECTION_TYPE_OPTIONS}
            />
            <small className="muted">Subscriptions open their sign-in flow after the connection is added.</small>
          </label>
        ) : null}
        {!editing ? (
          <label className="field">
            <span>Id</span>
            <Input
              value={draft.id}
              onChange={(event) => setDraft({ ...draft, id: event.target.value.trim().toLowerCase() })}
              placeholder="prod-gateway"
            />
            <small className={idError ? "provider-row__warn" : "muted"}>
              {idError || "Permanent — it appears inside model values like prod-gateway:protolabs/coder."}
            </small>
          </label>
        ) : null}
        <label className="field">
          <span>Name</span>
          <Input
            value={draft.label}
            onChange={(event) => setDraft({ ...draft, label: event.target.value })}
            placeholder={initial?.id ?? "Production gateway"}
          />
          <small className="muted">
            {initial
              ? `Display only. The permanent connection id remains ${initial.id}.`
              : "Display only. Change it whenever you like."}
          </small>
        </label>
        {draft.type === "openai-compat" ? (
          <>
            <label className="field">
              <span>Base URL</span>
              <Input
                value={draft.base_url}
                onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
                placeholder="https://api.example.com/v1"
              />
            </label>
            <label className="field">
              <span>API key</span>
              <SecretInput
                value={draft.api_key}
                placeholder={initial?.has_key ? "•••••••• — leave blank to keep" : "unset"}
                onChange={(event) => setDraft({ ...draft, api_key: event.target.value })}
              />
            </label>
          </>
        ) : (
          <small className="muted">
            Subscription endpoints and credentials are managed by sign-in. Choose which model uses this connection
            in the model slots after closing this dialog.
          </small>
        )}
      </form>
    </Dialog>
  );
}

// The resolve-and-remove dialog: one row per blocking reference, then a single DELETE that
// carries the operator's clear/repoint choices as `releases`. Built on the same `Dialog`
// as the add/edit form. Opened from the Remove button ONLY when `in_use` is non-empty; an
// unused connection is removed directly, exactly as before.
function ResolveReferencesDialog({
  provider,
  connections,
  isLast,
  onClose,
  onRemoved,
}: {
  provider: ProviderView;
  /** Every connection currently listed — the source for lane→display labels and for
   *  excluding the one being removed from the repoint options. */
  connections: ProviderView[];
  /** This is the last connection: the removal needs `confirm_last=true`, and the
   *  last-connection warning rides INLINE here (the two dialogs must never stack). */
  isLast: boolean;
  onClose: () => void;
  onRemoved: (removedId: string) => void;
}) {
  const toast = useToast();
  const inUse = provider.in_use ?? [];
  // Lane options come from the SAME source the settings model pickers use — the settings
  // schema's cross-provider, lane-qualified option list (ADR 0106 / bd-neiz) — NOT a new
  // probe. It loads (cached) on mount.
  const schema = useQuery(settingsSchemaQuery());
  const [selections, setSelections] = useState<ReferenceSelections>(() => defaultReferenceSelections(inUse));
  const [error, setError] = useState("");

  const picker = schema.data ? modelPickerData(schema.data.groups) : null;
  // Qualified `<pid>:<model>` options only (the cross-provider list) — the bare
  // single-lane fallback can't name a repoint target, so it is intentionally excluded.
  const qualified = picker?.crossProvider ?? [];
  const knownLanes = lanesFromOptions(qualified);
  const labelByPid = new Map(connections.map((c) => [c.id, c.display]));
  // Every OTHER connection's models, grouped by connection; the one being removed is
  // excluded — its models are about to disappear.
  const otherGroups = otherConnectionGroups(qualified, provider.id);
  const lanesLoading = schema.isLoading;

  // DropdownSelect is a flat radio group, so a disabled header row per connection provides
  // the "grouped by connection label" structure.
  const laneOptions = (withClear: boolean): DropdownSelectOption[] => {
    const opts: DropdownSelectOption[] = [];
    if (withClear) opts.push({ value: CLEAR_TARGET, label: "Clear (use lead model)" });
    for (const group of otherGroups) {
      opts.push({
        value: `__group:${group.lane}`,
        label: labelByPid.get(group.lane) ?? laneLabel(group.lane),
        disabled: true,
      });
      for (const item of group.items) opts.push({ value: item, label: bareModel(item, knownLanes) });
    }
    return opts;
  };

  const ready = referencesResolved(inUse, selections);

  const submit = useMutation({
    mutationFn: () => api.removeProvider(provider.id, isLast, buildReleases(inUse, selections)),
    onSuccess: (r) => {
      toast({ tone: "success", title: "Connection removed", message: r.removed });
      onRemoved(provider.id);
    },
    onError: (e) => {
      // 409 (a reference the operator left unresolved) or 400 (a bad repoint target): keep
      // the dialog open and show the server's exact `detail` inline, plus the toast.
      setError(errMsg(e));
      toast({ tone: "error", title: "Couldn't remove it", message: errMsg(e) });
    },
  });

  const close = () => {
    if (!submit.isPending) onClose();
  };

  return (
    <Dialog
      open
      onClose={close}
      title={`Remove ${provider.display}`}
      width="min(560px, 94vw)"
      className="provider-dialog"
      footer={
        <>
          <Button type="button" variant="ghost" onClick={close} disabled={submit.isPending}>
            Cancel
          </Button>
          <Button
            variant="danger"
            type="button"
            loading={submit.isPending}
            disabled={!ready || submit.isPending}
            onClick={() => {
              setError("");
              submit.mutate();
            }}
          >
            Repoint and remove {provider.display}
          </Button>
        </>
      }
    >
      <div className="provider-dialog__form" data-testid={`provider-resolve-${provider.id}`}>
        <p className="muted">
          These settings still use {provider.display}. Choose what each should use instead, then remove it.
        </p>

        {isLast ? (
          <Callout tone="warning">
            This is the last model connection. Removing it leaves this agent without a configured model source unless
            you add another first.
          </Callout>
        ) : null}

        {error ? <Callout tone="error">{error}</Callout> : null}

        {inUse.map((entry) => {
          const label = humaniseReferenceKey(entry.key);
          if (entry.kind === "favorite") {
            const favs = Array.isArray(entry.value) ? entry.value : [entry.value];
            return (
              <div className="field" key={entry.key} data-testid={`resolve-row-${entry.key}`}>
                <span>{label}</span>
                <Callout tone="neutral">
                  {favs.length} favorite{favs.length === 1 ? "" : "s"} will be removed: {favs.join(", ")}
                </Callout>
              </div>
            );
          }
          return (
            <label className="field" key={entry.key} data-testid={`resolve-row-${entry.key}`}>
              <span>{label}</span>
              <small className="muted">Currently {String(entry.value)}</small>
              <DropdownSelect
                aria-label={`New target for ${label}`}
                value={selections[entry.key] ?? ""}
                onValueChange={(v) => setSelections((s) => ({ ...s, [entry.key]: v }))}
                options={laneOptions(entry.clearable)}
                placeholder={lanesLoading ? "Loading models…" : "Choose a model…"}
                disabled={lanesLoading}
              />
              {!entry.clearable ? (
                <small className="muted">The lead model can&apos;t be cleared — choose another connection.</small>
              ) : null}
            </label>
          );
        })}
      </div>
    </Dialog>
  );
}
