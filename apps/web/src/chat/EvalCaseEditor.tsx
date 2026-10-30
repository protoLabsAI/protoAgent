import { Checkbox, Select, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useEffect, useRef, useState } from "react";
import { closeDocument, openDocument, useDocViewer } from "../docviewer";
import { evalCasesApi, type EvalCase, type EvalCaseSource } from "../lib/api/evalCases";

/** Shared entry point for chat and the friction-view handoff. Opening only drafts. */
export function openEvalCaseEditor(source: EvalCaseSource): void {
  const doc = {
    title: "Make eval case",
    render: () => <EvalCaseEditor key={JSON.stringify(source)} source={source} onClose={() => {
      if (useDocViewer.getState().doc === doc) closeDocument();
    }} />,
  };
  openDocument(doc);
}

const lines = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);
const message = (error: unknown) => error instanceof Error ? error.message : "Could not load or save the eval case.";

export function EvalCaseEditor({ source, onClose }: { source: EvalCaseSource; onClose: () => void }) {
  const [draft, setDraft] = useState<EvalCase | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setDraft(null);
    setError("");
    void evalCasesApi.draft(source).then((value) => {
      if (!cancelled) setDraft(value);
    }).catch((reason) => { if (!cancelled) setError(message(reason)); });
    return () => { cancelled = true; };
  }, [source.session_id, source.trace_id]);
  return draft ? <DraftForm key={draft.id} draft={draft} onClose={onClose} /> : <div>
    {error ? <p role="alert">{error}</p> : <p role="status">Loading draft…</p>}
    <Button onClick={onClose}>Cancel</Button>
  </div>;
}

function DraftForm({ draft, onClose }: { draft: EvalCase; onClose: () => void }) {
  const [prompt, setPrompt] = useState(draft.prompt);
  const [tier, setTier] = useState(draft.tier);
  const [tools, setTools] = useState(draft.expected_tools?.join("\n") ?? "");
  const [noTools, setNoTools] = useState(draft.expected_tools?.length === 0);
  const [forbidden, setForbidden] = useState(draft.forbidden_tools?.join("\n") ?? "");
  const [patterns, setPatterns] = useState(draft.expected_patterns?.join("\n") ?? "");
  const [forbiddenPatterns, setForbiddenPatterns] = useState(draft.forbidden_patterns?.join("\n") ?? "");
  const [rubric, setRubric] = useState(draft.verify_rubric?.criteria.join("\n") ?? "");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const busy = useRef(false);
  async function save() {
    if (busy.current) return;
    busy.current = true;
    setSaving(true);
    setError("");
    const edited: EvalCase = { ...draft, prompt, tier };
    // Blank fields omit assertions. Only an explicit choice asserts zero tool calls.
    delete edited.expected_tools;
    if (noTools || lines(tools).length) edited.expected_tools = noTools ? [] : lines(tools);
    edited.forbidden_tools = lines(forbidden);
    edited.expected_patterns = lines(patterns);
    edited.forbidden_patterns = lines(forbiddenPatterns);
    delete edited.verify_rubric;
    if (lines(rubric).length) edited.verify_rubric = { ...draft.verify_rubric, criteria: lines(rubric) };
    try {
      await evalCasesApi.save(edited);
      onClose();
    } catch (reason) {
      setError(message(reason));
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }
  return <form onSubmit={(event) => { event.preventDefault(); void save(); }} style={{ display: "grid", gap: 12 }}>
    {draft.comment ? <p>{draft.comment}</p> : null}
    <label>Prompt<Textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} disabled={saving} rows={4} /></label>
    <p>Enter one tool, reply pattern, or rubric criterion per line. Add at least one assertion before saving.</p>
    <label>Expected tools<Textarea value={tools} onChange={(event) => setTools(event.target.value)} disabled={saving || noTools} /></label>
    <Checkbox label="Expect no tool calls" checked={noTools} onCheckedChange={setNoTools} disabled={saving} />
    <label>Forbidden tools<Textarea value={forbidden} onChange={(event) => setForbidden(event.target.value)} disabled={saving} /></label>
    <label>Expected reply patterns<Textarea value={patterns} onChange={(event) => setPatterns(event.target.value)} disabled={saving} /></label>
    <label>Forbidden reply patterns<Textarea value={forbiddenPatterns} onChange={(event) => setForbiddenPatterns(event.target.value)} disabled={saving} /></label>
    <label>Rubric (optional)<Textarea value={rubric} onChange={(event) => setRubric(event.target.value)} disabled={saving} /></label>
    <label>Tier<Select value={tier} onChange={(event) => setTier(event.target.value as EvalCase["tier"])} disabled={saving}>
      <option value="gate">Gate</option><option value="tracked">Tracked</option>
    </Select></label>
    {error ? <p role="alert">{error}</p> : null}
    <div><Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>{" "}
      <Button type="button" onClick={onClose} disabled={saving}>Cancel</Button></div>
  </form>;
}
