import { Spinner } from "@protolabsai/ui/data";
import { Checkbox, FormField, Input } from "@protolabsai/ui/forms";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { useMutation } from "@tanstack/react-query";
import { ChevronRight, Search } from "lucide-react";
import { useState, type FormEvent } from "react";

import { api } from "../lib/api";
import { previewMcpSummary, previewSecretsSummary, requiresToolsNotice } from "../lib/archetypeConfig";
import { parseBundleUrl } from "../lib/bundleUrl";
import { errMsg } from "../lib/format";
import { lucideIcon } from "../lib/lucideIcon";
import type { ArchetypeFromUrl } from "../lib/types";
import { RunsCodeWarning } from "../plugins/TrustAckDialog";
import { MemberCard } from "../setup/ArchetypePreviewDialog";
import "./bundleUrl.css";

// The New-agent panel's third source: an archetype bundle that ISN'T in the catalog, by its
// git URL (+ optional ref). Three beats, in order, and nothing installs until the last:
//   1. URL ENTRY — validated here (lib/bundleUrl) and again server-side.
//   2. PREVIEW + TRUST — GET /api/archetypes/from-url peeks the bundle (read-only): its
//      archetype card, its description, and what it installs (each plugin + its ref, the
//      built-ins it turns on, what it will ask for). Installing third-party code is a trust
//      decision, so a source that isn't official/acked needs an explicit "I trust this
//      repository" before Next — the same "this runs code" copy as the plugin install ack.
//   3. SET UP — `onNext` hands the result to the panel, which runs the SAME set-up dialog
//      the catalog cards use and creates with `bundle` + `ref`.
// Editing the URL or ref after a lookup drops the preview, so Next always means the bundle
// that was actually shown.
export function BundleUrlSource({ onNext }: { onNext: (found: ArchetypeFromUrl) => void }) {
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [trustAck, setTrustAck] = useState(false);
  const lookup = useMutation({
    mutationFn: (q: { url: string; ref?: string }) => api.archetypeFromUrl(q.url, q.ref),
    onMutate: () => setTrustAck(false),
  });
  const found = lookup.data;

  function edit(next: { url?: string; ref?: string }) {
    if (next.url !== undefined) setUrl(next.url);
    if (next.ref !== undefined) setRef(next.ref);
    setInputError(null);
    if (lookup.data || lookup.error) lookup.reset();
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const parsed = parseBundleUrl(url, ref);
    if (!parsed.ok) {
      setInputError(parsed.error);
      return;
    }
    // Show the operator what will actually be fetched (a /tree/<ref> page URL folds).
    setUrl(parsed.url);
    setRef(parsed.ref ?? "");
    lookup.mutate({ url: parsed.url, ref: parsed.ref });
  }

  const canNext = Boolean(found) && (found?.trusted || trustAck);

  return (
    <div className="bundle-url-source">
      <p className="fleet-section-label">Bundle URL</p>
      <p className="archetype-preview-muted">
        Create an agent from an archetype bundle that isn&apos;t in the list — paste its git repository URL. You&apos;ll
        see what it installs before anything runs.
      </p>
      <form className="bundle-url-form" onSubmit={submit} noValidate>
        <FormField label="Repository URL">
          <Input
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder="https://github.com/owner/some-archetype"
            value={url}
            onChange={(e) => edit({ url: e.target.value })}
            aria-invalid={inputError ? true : undefined}
          />
        </FormField>
        <FormField label="Ref (optional)">
          <Input
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="v0.1.0"
            value={ref}
            onChange={(e) => edit({ ref: e.target.value })}
          />
        </FormField>
        <div className="bundle-url-actions">
          <Button type="submit" variant="default" disabled={!url.trim() || lookup.isPending}>
            {lookup.isPending ? <Spinner size={14} /> : <Search size={14} />} Look up
          </Button>
        </div>
      </form>
      <p className="archetype-preview-muted bundle-url-hint">
        Ref is a tag, branch or commit SHA — blank uses the default branch. A GitHub <code>/tree/&lt;ref&gt;</code> link
        works too.
      </p>
      {inputError ? (
        <p className="bundle-url-error" role="alert">
          {inputError}
        </p>
      ) : null}
      {lookup.isError ? (
        <p className="bundle-url-error" role="alert">
          Couldn&apos;t read that bundle — {errMsg(lookup.error)}
        </p>
      ) : null}

      {found ? <BundleFoundPreview found={found} trustAck={trustAck} onTrustAck={setTrustAck} /> : null}

      <div className="panel-actions archetype-step-actions">
        <Button variant="primary" disabled={!canNext} onClick={() => found && onNext(found)}>
          Next
          <ChevronRight size={15} />
        </Button>
      </div>
    </div>
  );
}

// Step 2: what this bundle is and what creating from it installs — then the trust decision.
function BundleFoundPreview({
  found,
  trustAck,
  onTrustAck,
}: {
  found: ArchetypeFromUrl;
  trustAck: boolean;
  onTrustAck: (on: boolean) => void;
}) {
  const a = found.archetype;
  const bundle = found.bundle;
  const members = bundle?.members ?? [];
  const configLabels = (bundle?.config_inputs ?? []).map((c) => c.label || c.key);
  const contract = requiresToolsNotice(a.label, a.requires_tools);
  return (
    <section className="bundle-url-preview" aria-label={`${a.label} bundle preview`}>
      <div className="bundle-url-preview-head">
        <span className="bundle-url-preview-icon" aria-hidden>
          {lucideIcon(a.icon, 22)}
        </span>
        <div>
          <strong>{a.label}</strong>
          <div className="archetype-preview-muted">
            <code>
              {found.source}
              {a.ref ? `@${a.ref}` : ""}
            </code>
            {a.ref ? null : " · default branch"}
          </div>
        </div>
      </div>
      {a.blurb ? <p className="archetype-preview-desc">{a.blurb}</p> : null}
      {bundle?.description && bundle.description !== a.blurb ? (
        <p className="archetype-preview-desc">{bundle.description}</p>
      ) : null}

      <p className="fleet-section-label">What it installs</p>
      {members.length ? (
        <div className="archetype-preview-members">
          {members.map((m, i) => (
            <MemberCard key={m.id ?? i} member={m} />
          ))}
        </div>
      ) : (
        <p className="archetype-preview-muted">No plugins — a persona-only bundle.</p>
      )}
      {configLabels.length ? (
        <p className="archetype-preview-muted">It will ask for: {configLabels.join(", ")}</p>
      ) : null}
      {bundle?.mcp?.length ? (
        <p className="archetype-preview-muted">MCP servers: {previewMcpSummary(bundle.mcp)}</p>
      ) : null}
      {bundle?.secrets?.length ? (
        <p className="archetype-preview-muted">Secrets: {previewSecretsSummary(bundle.secrets)}</p>
      ) : null}
      {contract ? (
        <p className="archetype-runtime-notice" role="note">
          {contract}
        </p>
      ) : null}

      {found.trusted ? (
        <Callout tone="info" title="Official source">
          Creating this agent installs the plugins above into it and turns them on.
        </Callout>
      ) : (
        <Callout tone="warning" title="This bundle runs code on your machine">
          <p>
            <RunsCodeWarning source={found.source} doing="Creating this agent installs its plugins and" />
          </p>
          <Checkbox checked={trustAck} onCheckedChange={(v: boolean) => onTrustAck(Boolean(v))} label="I trust this repository" />
        </Callout>
      )}
    </section>
  );
}
