import { Button } from "@protolabsai/ui/primitives";
import { AlertTriangle, HardDrive, RefreshCw, Trash2 } from "lucide-react";
import { useState, useSyncExternalStore } from "react";

// The storage seam is import-free by contract (lib/storage.ts), so the crash page may use it.
import {
  devHooksEnabled,
  entryBytes,
  isQuotaError,
  keySizes,
  listKeys,
  matchKey,
  readKey,
  removeKey,
  subscribeDevHooks,
} from "../lib/storage";
import { blockPersistInThisPage, broadcastStorageReset } from "../lib/storageReset";
import "./app-crash.css";

// Full-page fallback for the ROOT error boundary (#872) — a render throw that
// escapes every panel boundary lands here instead of a white screen. This renders
// while the app is broken, so it must stay dependency-light: no stores, no
// queries, no router — any of them may be the thing that threw.

type NoFlushGlobals = { __protoagentForceQuotaCrash?: boolean };

/** Clear the persisted chat sessions (all agents' slug-suffixed keys included) but
 *  keep layout/theme/authToken — the same selective scope as the tenant guard. A
 *  corrupt saved session is the known way to brick render (issue #872); everything
 *  else persisted is cheap to keep. */
export function resetChatData() {
  blockPersistInThisPage();
  listKeys("local")
    .filter((k) => k.startsWith("protoagent.chat.sessions"))
    .forEach((k) => removeKey("local", k));
}

/** Below this, clearing transcripts didn't fix a full quota — the hog is something else
 *  (a plugin, the design system), so show the operator the largest keys instead of looping
 *  through reload back into the crash. */
export const MIN_FREED_BYTES = 64 * 1024;

/** "Free up space" (ADR 0114 D6): clear ONLY localStorage transcript-category keys (chat
 *  session blobs, palette/DM threads — by exact registry pattern, so `.dismissed` sets,
 *  auth, theme and layout survive), stop this page's chat flush, and tell other tabs.
 *  Returns the bytes freed. */
export function freeTranscriptSpace(): number {
  blockPersistInThisPage();
  let freed = 0;
  for (const key of listKeys("local")) {
    if (matchKey("local", key)?.spec.category !== "transcript") continue;
    const value = readKey("local", key);
    removeKey("local", key);
    if (value !== null && readKey("local", key) === null) freed += entryBytes(key, value);
  }
  broadcastStorageReset();
  return freed;
}

function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** e2e/QA hook (ADR 0114): with `globalThis.__protoagentForceQuotaCrash` set, render throws a
 *  quota error so the recovery path stays testable now that the storage seam never lets a
 *  real one reach a render. Rendered inside the root boundary by main.tsx; inert unless the
 *  storage dev hooks are live (a dev build or a non-prod channel — never in production). */
export function ForcedQuotaCrash() {
  const enabled = useSyncExternalStore(subscribeDevHooks, devHooksEnabled, devHooksEnabled);
  if (enabled && (globalThis as NoFlushGlobals).__protoagentForceQuotaCrash) {
    const err = new Error("The quota has been exceeded.");
    err.name = "QuotaExceededError";
    throw err;
  }
  return null;
}

/** Credentials and the tenant stamp: listed (their size counts), but never one click away —
 *  clearing a remote operator's token from a crash page locks them out. */
const PROTECTED = new Set(["auth", "tenant"]);

function LargestKeys({ freed }: { freed: number }) {
  const [rows, setRows] = useState(() => keySizes("local").slice(0, 12));
  return (
    <div className="app-crash__keys">
      <p className="app-crash__hint">
        Clearing chat history freed only {formatBytes(freed)} — something else is filling this
        site&apos;s browser storage. The largest entries are below; clear what you don&apos;t need,
        then reload.
      </p>
      <ul>
        {rows.map((r) => (
          <li key={r.key}>
            <code title={r.key}>{r.key}</code>
            <span>{formatBytes(r.bytes)}</span>
            {PROTECTED.has(matchKey("local", r.key)?.spec.category ?? "") ? (
              <span className="app-crash__kept">kept</span>
            ) : (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              aria-label={`Clear ${r.key}`}
              onClick={() => {
                removeKey("local", r.key);
                setRows(keySizes("local").slice(0, 12));
              }}
            >
              <Trash2 size={12} /> Clear
            </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AppCrash({
  error,
  reload = () => window.location.reload(),
}: {
  error: Error;
  /** Injected by tests (jsdom's location.reload can't be stubbed). */
  reload?: () => void;
}) {
  const quota = isQuotaError(error);
  const [freed, setFreed] = useState<number | null>(null);
  return (
    <div className="app-crash" role="alert">
      <AlertTriangle size={28} aria-hidden />
      <h1>{quota ? "This browser's storage for the console is full" : "The console hit a render error"}</h1>
      <p className="app-crash__msg">{error.message}</p>
      <div className="app-crash__actions">
        {quota && (
          <Button
            type="button"
            onClick={() => {
              const n = freeTranscriptSpace();
              if (n >= MIN_FREED_BYTES) reload();
              else setFreed(n);
            }}
          >
            <HardDrive size={14} /> Free up space &amp; reload
          </Button>
        )}
        <Button type="button" variant={quota ? "ghost" : undefined} onClick={reload}>
          <RefreshCw size={14} /> Reload
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            resetChatData();
            reload();
          }}
        >
          <Trash2 size={14} /> Reset chat data &amp; reload
        </Button>
      </div>
      {freed !== null && <LargestKeys freed={freed} />}
      <p className="app-crash__hint">
        {quota
          ? "Free up space clears this browser's saved chat transcripts (the server keeps the last 24 h). Layout, theme, auth token and dismissals are kept."
          : "Layout, theme and auth token are kept. If reloading loops back here, reset chat data — a corrupt saved session is the usual cause."}
      </p>
    </div>
  );
}
