import "./chat-component.css";

import type { JSX } from "react";

import { registeredChatComponents } from "../ext/componentRegistry";
import type { ComponentSpec } from "../lib/types";

// Curated, data-only chat component registry (ADR 0051 Slice 2). Renders typed
// component-v1 DataParts inline in the transcript. No code execution — props are pure
// data, so this is safe without a sandbox (free-form generated UI uses the ADR 0038
// iframe/artifact path instead). Unknown component types degrade to a labeled note.
// The built-ins below are EXTENSIBLE: forks/plugins add (or override) renderers via the
// `registerChatComponent` ext seam (#1323), so the agent's component vocabulary grows
// without editing this file.

type Row = unknown[];

function asString(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function Title({ props }: { props: Record<string, unknown> }) {
  const t = typeof props.title === "string" ? props.title : "";
  return t ? <div className="chat-comp-title">{t}</div> : null;
}

function TableComponent({ props }: { props: Record<string, unknown> }) {
  const columns = Array.isArray(props.columns) ? (props.columns as unknown[]).map(asString) : [];
  const rows = Array.isArray(props.rows) ? (props.rows as Row[]) : [];
  return (
    <div className="chat-comp chat-comp-table">
      <Title props={props} />
      <table>
        {columns.length > 0 ? (
          <thead>
            <tr>
              {columns.map((c, i) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
        ) : null}
        <tbody>
          {rows.map((r, ri) => (
            <tr key={ri}>
              {(Array.isArray(r) ? r : [r]).map((cell, ci) => (
                <td key={ci}>{asString(cell)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KeyValueComponent({ props }: { props: Record<string, unknown> }) {
  const items = Array.isArray(props.items)
    ? (props.items as Array<Record<string, unknown>>)
    : Object.entries((props.pairs as Record<string, unknown>) || {}).map(([label, value]) => ({
        label,
        value,
      }));
  return (
    <div className="chat-comp chat-comp-kv">
      <Title props={props} />
      <dl>
        {items.map((it, i) => (
          <div className="chat-comp-kv-row" key={i}>
            <dt>{asString(it.label)}</dt>
            <dd>{asString(it.value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function TimelineComponent({ props }: { props: Record<string, unknown> }) {
  const steps = Array.isArray(props.steps) ? (props.steps as Array<Record<string, unknown>>) : [];
  return (
    <div className="chat-comp chat-comp-timeline">
      <Title props={props} />
      <ol>
        {steps.map((s, i) => {
          const state = asString(s.state) || "todo";
          return (
            <li key={i} className={`chat-comp-step is-${state}`}>
              <span className="chat-comp-step-dot" aria-hidden />
              <span className="chat-comp-step-body">
                <span className="chat-comp-step-label">{asString(s.label)}</span>
                {s.detail ? <span className="chat-comp-step-detail">{asString(s.detail)}</span> : null}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

const BUILTINS: Record<string, (p: { props: Record<string, unknown> }) => JSX.Element> = {
  table: TableComponent,
  keyvalue: KeyValueComponent,
  timeline: TimelineComponent,
};

export function ChatComponent({ spec }: { spec: ComponentSpec }) {
  // Registered (fork/plugin) renderers win over the built-ins of the same name, so a fork can
  // both add new kinds and re-skin a built-in (#1323).
  const Renderer = registeredChatComponents()[spec.component] ?? BUILTINS[spec.component];
  if (!Renderer) {
    return <div className="chat-comp chat-comp-unknown">[unsupported component: {spec.component}]</div>;
  }
  try {
    return <Renderer props={spec.props || {}} />;
  } catch {
    return <div className="chat-comp chat-comp-unknown">[failed to render {spec.component}]</div>;
  }
}
