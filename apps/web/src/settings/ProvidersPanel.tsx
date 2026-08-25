import "./settings.css";
import "./providers.css";

import { Input, SecretInput } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
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
  "openai-codex": "ChatGPT subscription",
};

const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

export function ProvidersPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["providers"], queryFn: () => api.providers() });
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ id: "", type: "openai-compat", label: "", base_url: "", api_key: "" });
  const [models, setModels] = useState<Record<string, string[]>>({});

  const refresh = () => void qc.invalidateQueries({ queryKey: ["providers"] });

  const add = useMutation({
    mutationFn: () => api.addProvider(draft),
    onSuccess: () => {
      toast({ tone: "success", title: "Connection added", message: `${draft.id} is ready to use in model slots.` });
      setAdding(false);
      setDraft({ id: "", type: "openai-compat", label: "", base_url: "", api_key: "" });
      refresh();
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't add the connection", message: errMsg(e) }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.removeProvider(id),
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

  const idError =
    draft.id && !ID_RE.test(draft.id)
      ? "Lowercase letters, digits, - and _ only — ':' and '/' are reserved by the provider:model grammar."
      : "";

  const rows = data?.providers ?? [];

  return (
    <div className="settings-subsection settings-subsection--lead" data-testid="providers-panel">
      <h2 className="panel-kicker">Connections</h2>
      <p className="muted">
        Every model source this agent can use. Model slots pick from all of them — there is no
        default to switch between.
      </p>

      {isLoading ? <p className="muted">Loading…</p> : null}

      {rows.map((p) => (
        <div key={p.id} className="provider-row" data-testid="provider-row">
          <div className="provider-row__head">
            <div>
              <span className="provider-row__name">{p.display}</span>
              <code className="provider-row__id">{p.id}</code>
              <Badge>{TYPE_LABEL[p.type] ?? p.type}</Badge>
            </div>
            <div className="provider-row__actions">
              <Button size="sm" variant="ghost" onClick={() => probe.mutate(p.id)} disabled={probe.isPending}>
                Test
              </Button>
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Remove ${p.display}`}
                onClick={() => remove.mutate(p.id)}
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
            <div className="provider-row__meta">
              {p.has_key ? (
                <span className="provider-row__ok">
                  <Check size={12} /> key stored
                </span>
              ) : (
                <span className="provider-row__warn">no API key — slots naming it will fail</span>
              )}
            </div>
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

      {adding ? (
        <div className="provider-row provider-row--draft">
          <label className="field">
            <span>Id</span>
            <Input
              value={draft.id}
              onChange={(e) => setDraft({ ...draft, id: e.target.value.trim().toLowerCase() })}
              placeholder="prod-gateway"
            />
            {/* Frozen after creation: an id lives inside stored model values, and via the
                fleet host layer those can sit in another instance's config a rename can't reach. */}
            <small className={idError ? "provider-row__warn" : "muted"}>
              {idError || "Permanent — it appears inside model values like prod-gateway:protolabs/coder."}
            </small>
          </label>
          <label className="field">
            <span>Name</span>
            <Input
              value={draft.label}
              onChange={(e) => setDraft({ ...draft, label: e.target.value })}
              placeholder="Production gateway"
            />
            <small className="muted">Display only. Change it whenever you like.</small>
          </label>
          <label className="field">
            <span>Base URL</span>
            <Input
              value={draft.base_url}
              onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
              placeholder="https://api.example.com/v1"
            />
          </label>
          <label className="field">
            <span>API key</span>
            <SecretInput
              value={draft.api_key}
              onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
            />
          </label>
          <div className="provider-row__actions">
            <Button size="sm" onClick={() => add.mutate()} disabled={!draft.id || !!idError || add.isPending}>
              Add connection
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <Button size="sm" variant="ghost" onClick={() => setAdding(true)} data-testid="add-provider">
          <Plus size={14} /> Add a connection
        </Button>
      )}
    </div>
  );
}
