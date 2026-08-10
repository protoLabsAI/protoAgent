import "./pathpicker.css";

import { Spinner } from "@protolabsai/ui/data";
import { Input } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ChevronRight, File, Folder, FolderOpen } from "lucide-react";
import { useEffect, useState } from "react";

import { api, isHostConsole } from "../lib/api";
import { canPickNatively, hasDesktopShell, pickPathNative } from "../lib/desktop";
import { errMsg } from "../lib/format";
import type { BrowseEntry } from "../lib/types";

// Folder/file picker for every path-valued setting — Work folders (ADR 0007), the
// project dir, the checkpoint DB. Typing is the one input method that can't tell you
// the path doesn't exist, and a bad path is expensive here: an unusable work folder is
// skipped at graph build, and if it was the only one the WHOLE fs toolset unbinds.
// Picking can only ever produce a directory that's really there.
//
// It browses the SERVER's filesystem, not the browser's. The console frequently
// configures a machine it isn't running on (tailnet, fleet members, Docker), and the
// browser-native pickers describe the wrong one: `webkitdirectory` yields the client's
// files under a fake root, `showDirectoryPicker()` an opaque handle with no path. So
// the server lists (GET /api/fs/browse) and this walks it. The text input stays
// editable — pasting a known path is still the fastest route for someone who knows it.

// Native-looking path examples (#2470): a POSIX ~/Documents placeholder on a
// Windows host reads as untested/unsupported. navigator.platform ≈ the host in
// the desktop app (the dominant case); a cross-platform remote console only
// sees a placeholder example, never a wrong behavior.
const IS_WIN = typeof navigator !== "undefined" && /win/i.test(navigator.platform || "");
const DIR_EXAMPLE = IS_WIN ? "C:\\Users\\you\\Documents" : "~/Documents";
const FILE_EXAMPLE = IS_WIN ? "C:\\path\\to\\file" : "/path/to/file";

export function PathPicker({
  value,
  onChange,
  kind = "dir",
  id,
  placeholder,
  ariaLabel,
  invalid,
}: {
  value: string;
  onChange: (v: string) => void;
  kind?: "dir" | "file";
  id?: string;
  placeholder?: string;
  ariaLabel?: string;
  invalid?: boolean;
}) {
  const [open, setOpen] = useState(false);

  // Desktop host window only (#2265) — see canPickNatively for why a fleet-member window
  // keeps the server-side browser. Read once per render: both inputs are fixed for the
  // life of the window (the Tauri global and the URL slug).
  const native = canPickNatively(hasDesktopShell(), isHostConsole());

  const browse = async () => {
    if (native) {
      const picked = await pickPathNative({ start: value, files: kind === "file" });
      // undefined = no usable native picker after all (a shell too old for the command);
      // fall through to the in-app browser so Browse… is never a dead button.
      if (picked !== undefined) {
        if (picked) onChange(picked); // null = cancelled → leave the field alone
        return;
      }
    }
    setOpen(true);
  };

  return (
    <>
      <div className="path-picker">
        <Input
          id={id}
          className="setting-input path-picker-input"
          value={value}
          placeholder={placeholder ?? (kind === "file" ? FILE_EXAMPLE : DIR_EXAMPLE)}
          aria-label={ariaLabel}
          aria-invalid={invalid}
          onChange={(e) => onChange(e.target.value)}
        />
        <Button variant="ghost" size="sm" type="button" onClick={() => void browse()}>
          <FolderOpen size={15} /> Browse…
        </Button>
      </div>
      {open ? (
        <BrowseDialog
          kind={kind}
          start={value}
          onClose={() => setOpen(false)}
          onPick={(picked) => {
            onChange(picked);
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}

function BrowseDialog({
  kind,
  start,
  onPick,
  onClose,
}: {
  kind: "dir" | "file";
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  // `cwd` null = "let the server decide" (it starts at home). Seeding from the current
  // value would 404 the moment the field holds a typo — which is exactly when someone
  // reaches for Browse — so the seed is applied only after a successful first load.
  const [cwd, setCwd] = useState<string | null>(start.trim() || null);
  const [selected, setSelected] = useState<string | null>(null);
  const [fellBack, setFellBack] = useState(false);

  const q = useQuery({
    queryKey: ["fs-browse", cwd, kind],
    queryFn: () => api.browseDir({ path: cwd ?? "", files: kind === "file" }),
    retry: false,
    // Hold the previous listing while the next one loads: descending a tree otherwise
    // blanks the list on every step, which reads as "this folder is empty" for a beat.
    // Confirm is disabled while fetching (below) so the stale path can't be submitted.
    placeholderData: (prev) => prev,
  });

  // A seeded path that doesn't resolve (stale config, deleted folder, typo) must not
  // dead-end the picker — drop to the server default once and say why.
  useEffect(() => {
    if (q.isError && cwd !== null && !fellBack) {
      setFellBack(true);
      setCwd(null);
    }
  }, [q.isError, cwd, fellBack]);

  const data = q.data;
  // Folder mode confirms wherever you've navigated to; file mode needs an explicit file.
  const chosen = kind === "dir" ? data?.path ?? null : selected;

  return (
    <Dialog open onClose={onClose} title={kind === "file" ? "Choose a file" : "Choose a folder"} width={560}>
      <div className="path-browser">
        {data?.roots?.length ? (
          <div className="path-browser-roots">
            {data.roots.map((r) => (
              <Button
                key={r.path}
                variant="ghost"
                size="sm"
                type="button"
                onClick={() => {
                  setSelected(null);
                  setCwd(r.path);
                }}
              >
                {r.label}
              </Button>
            ))}
          </div>
        ) : null}

        <div className="path-browser-bar">
          <Button
            variant="ghost"
            size="sm"
            type="button"
            disabled={!data?.parent}
            aria-label="Up one folder"
            onClick={() => {
              setSelected(null);
              if (data?.parent) setCwd(data.parent);
            }}
          >
            ← Up
          </Button>
          <code className="path-browser-cwd" title={data?.path ?? ""}>
            {data?.path ?? "…"}
          </code>
        </div>

        {fellBack && !q.isError ? (
          <p className="path-browser-note" role="status">
            <AlertTriangle size={13} /> <span>That path doesn’t exist — starting from your home folder.</span>
          </p>
        ) : null}
        {data?.truncated ? (
          <p className="path-browser-note" role="status">
            <AlertTriangle size={13} />{" "}
            <span>
              Showing the first {data.entries.length} entries — this folder has more. Type the path
              directly if what you want isn’t listed.
            </span>
          </p>
        ) : null}

        <div className="path-browser-list" role="listbox" aria-label="Folder contents">
          {q.isFetching && !data ? (
            <p className="path-browser-empty">
              <Spinner size={16} /> <span>Loading…</span>
            </p>
          ) : q.isError ? (
            <p className="path-browser-empty" role="alert">
              <AlertTriangle size={14} /> <span>{errMsg(q.error)}</span>
            </p>
          ) : !data?.entries.length ? (
            <p className="path-browser-empty">This folder is empty.</p>
          ) : (
            // A folder row NAVIGATES on a single click (no select-vs-descend ambiguity,
            // and no nested click target a keyboard can't reach): you walk to the folder
            // you want and confirm with "Use this folder". Only in file mode does a row
            // select, and only a file row.
            data.entries.map((entry: BrowseEntry) => {
              const isDir = entry.kind === "dir";
              const active = selected === entry.path;
              return (
                <button
                  key={entry.path}
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`path-browser-row${active ? " is-selected" : ""}`}
                  onClick={() => {
                    if (isDir) {
                      setSelected(null);
                      setCwd(entry.path);
                    } else {
                      setSelected(entry.path);
                    }
                  }}
                >
                  {isDir ? <Folder size={15} /> : <File size={15} />}
                  <span className="path-browser-name">{entry.name}</span>
                  {isDir ? <ChevronRight className="path-browser-into" size={14} aria-hidden /> : null}
                </button>
              );
            })
          )}
        </div>

        <div className="path-browser-actions">
          <code className="path-browser-chosen">{chosen ?? "nothing selected"}</code>
          <div className="path-browser-buttons">
            <Button variant="ghost" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant="primary"
              type="button"
              // Never confirm mid-navigation: with the previous listing held, `chosen`
              // is still the OLD folder until the new one lands.
              disabled={!chosen || q.isFetching}
              onClick={() => chosen && onPick(chosen)}
            >
              {kind === "file" ? "Select file" : "Use this folder"}
            </Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
