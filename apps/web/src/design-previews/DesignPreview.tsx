import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { Select } from "@protolabsai/ui/forms";

import { openView } from "../app/palette/nav";
import { request } from "../lib/api/http";
import { apiUrl, currentSlug } from "../lib/api/routing";
import { postToPluginView } from "../lib/pluginViewInbox";
import { parsePreview, previewRef, type Palette, type Contrast, type ComponentPreview, type Swatch } from "./preview";
import "./design-preview.css";

function Copy({ value, label = "Copy value" }: { value: string; label?: string }) {
  const [status, setStatus] = useState("");
  return <span className="design-preview__copy">
    <Button size="sm" variant="ghost" aria-label={`${label}: ${value}`} onClick={async () => {
      try { await navigator.clipboard.writeText(value); setStatus("Copied"); }
      catch { setStatus("Could not copy — select the value instead"); }
    }}>{label}</Button>
    <span role="status">{status}</span>
  </span>;
}

function SwatchGroup({ group, mode, open }: { group: Palette["groups"][number]; mode: "dark" | "light"; open: boolean }) {
  const [showAll, setShowAll] = useState(false);
  const visible: Swatch[] = showAll ? group.swatches : group.swatches.slice(0, 6);
  return <details open={open} className="design-preview__group">
    <summary>{group.name} · {group.swatches.length} colors</summary>
    <div className="design-preview__swatches">{visible.map((s, j) => {
      const value = mode === "light" ? s.light ?? s.value : s.value;
      return <div className="design-preview__swatch" key={j}>
        <span className="design-preview__color" style={{ backgroundColor: value }} aria-hidden="true" />
        <span className="design-preview__label">{s.label}</span>
        <code>{value}</code>
        {s.light !== undefined && <span className="design-preview__pair">Dark {s.value} · Light {s.light}</span>}
        <div className="design-preview__actions">
          <Copy key={value} value={value} />
          {s.copy && <Copy value={s.copy} label="Copy token" />}
        </div>
      </div>;
    })}</div>
    {group.swatches.length > 6 && <Button size="sm" variant="ghost" aria-expanded={showAll}
      onClick={() => setShowAll(!showAll)}>{showAll ? "Show fewer colors" : `Show all ${group.swatches.length} colors`}</Button>}
  </details>;
}

function PaletteCard({ data }: { data: Palette }) {
  const [mode, setMode] = useState(data.mode);
  const paired = data.groups.some(g => g.swatches.some(s => s.light !== undefined));
  return <>
    {paired && <div className="design-preview__actions" aria-label="Palette appearance">
      {(["dark", "light"] as const).map(m => <Button key={m} size="sm" variant="ghost"
        aria-pressed={mode === m} onClick={() => setMode(m)}>{m === "dark" ? "Dark" : "Light"}</Button>)}
    </div>}
    {data.groups.map((g, i) => <SwatchGroup key={i} group={g} mode={mode} open={i === 0} />)}
  </>;
}

function ContrastCard({ data }: { data: Contrast }) {
  return <>
    <div className="design-preview__specimen" style={{ color: data.foreground, backgroundColor: data.background }}>
      <strong>Aa — The quick brown fox</strong><span>Sample text on the proposed background.</span>
    </div>
    <p><code>{data.foreground}</code> on <code>{data.background}</code> · <strong>{data.ratio}:1</strong></p>
    <ul>{([["AA text", data.aa_normal], ["AA large text", data.aa_large], ["AAA text", data.aaa_normal]] as const)
      .map(([label, pass]) => <li key={label}>{label}: {pass ? "Pass" : "Fail"}</li>)}</ul>
    <div className="design-preview__actions"><Copy value={data.foreground} label="Copy foreground" /><Copy value={data.background} label="Copy background" /></div>
  </>;
}

function ComponentCard({ data }: { data: ComponentPreview }) {
  const [selected, setSelected] = useState(data.stories[0].id);
  const story = data.stories.find(s => s.id === selected) ?? data.stories[0];
  const [mode, setMode] = useState("dark");
  const url = new URL(story.preview);
  url.searchParams.set("globals", `theme:${mode}`);
  // Same-origin script+same-origin sandbox can escape its sandbox. Never embed the host.
  const hostOrigins = [window.location.origin, new URL(apiUrl("/"), window.location.href).origin];
  const canEmbed = !hostOrigins.includes(url.origin);
  return <>
    <p className="design-preview__note">Live Storybook preview — reflects the current published component.</p>
    <div className="design-preview__actions">
      <Select aria-label="Component variant" value={story.id} onChange={e => setSelected(e.target.value)}>
        {data.stories.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
      </Select>
      <Select aria-label="Component appearance" value={mode} onChange={e => setMode(e.target.value)}>
        <option value="dark">Dark</option><option value="light">Light</option>
      </Select>
    </div>
    {canEmbed && <iframe key={url.href} className="design-preview__frame" src={url.href}
      title={`${data.title} — ${story.name}`} loading="lazy" sandbox="allow-scripts allow-same-origin" referrerPolicy="no-referrer" />}
    <div className="design-preview__actions">
      <Button size="sm" onClick={() => {
        const key = "plugin:design-system:design-system";
        postToPluginView(key, { type: "design-system:select-story", story_id: story.id });
        openView(key);
      }}>Open playground</Button>
      <a href={story.docs} target="_blank" rel="noopener noreferrer">Open in Storybook ↗</a>
    </div>
    <p className="design-preview__note">If the preview cannot load, open it in Storybook.</p>
  </>;
}

export function DesignPreview({ props }: { props: Record<string, unknown> }) {
  const ref = previewRef(props);
  const query = useQuery({
    queryKey: ["design-preview", currentSlug(), ref?.id],
    queryFn: async ({ signal }) => parsePreview(await request<unknown>(`/api/plugins/design-system/previews/${ref!.id}`, { signal })),
    enabled: !!ref, staleTime: Infinity, retry: false,
  });
  const data = query.data;
  return <section className="chat-comp design-preview" aria-label={ref?.title ?? "Design preview"}>
    <div className="chat-comp-title">{ref?.title ?? "Design preview"}</div>
    {!ref ? <p>Invalid design preview reference.</p> : query.isPending ? <p role="status">Loading preview…</p>
      : query.isError || !data ? <p>Preview unavailable. The design-system plugin or saved preview may no longer be available.</p>
      : <>
        {data.kind === "palette" ? <PaletteCard key={ref.id} data={data} />
          : data.kind === "contrast" ? <ContrastCard data={data} /> : <ComponentCard key={ref.id} data={data} />}
        {data.kind !== "component" && <p className="design-preview__note">Saved preview · <time dateTime={data.created_at}>{data.created_at.slice(0, 10)}</time></p>}
      </>}
  </section>;
}
