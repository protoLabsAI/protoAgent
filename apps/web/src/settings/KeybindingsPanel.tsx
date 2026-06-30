import "./keybindings.css";

import { Button, Kbd } from "@protolabsai/ui/primitives";
import { useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import type { Keybinding } from "../ext/keybindingRegistry";
import { eventToCombo, formatCombo } from "../keybindings/combo";
import { useKbIntents } from "../keybindings/intents";
import { effectiveCombo, useKeybindingOverrides } from "../keybindings/overrides";

// Settings ▸ Keyboard (ADR 0063) — view + rebind every registered keybinding. Click a
// shortcut to record a new combo; conflicts (same combo in an overlapping scope) are
// blocked with a note. Overrides persist globally; reset per-row or all at once.

// Two scopes "overlap" (and thus conflict on the same combo) when either is global, or
// they're the same panel — i.e. they could both be active for one keypress.
function scopesOverlap(a: string | undefined, b: string | undefined): boolean {
  return !a || !b || a === b;
}

export function KeybindingsPanel() {
  const overrides = useKeybindingOverrides((s) => s.overrides);
  const setBinding = useKeybindingOverrides((s) => s.setBinding);
  const resetBinding = useKeybindingOverrides((s) => s.resetBinding);
  const resetAll = useKeybindingOverrides((s) => s.resetAll);
  const setCapturing = useKbIntents((s) => s.setCapturing);
  const [recordingId, setRecordingId] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ id: string; with: string } | null>(null);

  const bindings = registeredKeybindings();
  const groups = [...new Set(bindings.map((b) => b.group || "Other"))];

  function startRecording(id: string) {
    setRecordingId(id);
    setConflict(null);
    setCapturing(true); // mute the global host while we capture
  }
  function stopRecording() {
    setRecordingId(null);
    setCapturing(false);
  }

  function onRecordKey(b: Keybinding, e: ReactKeyboardEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      stopRecording();
      return;
    }
    const combo = eventToCombo(e.nativeEvent);
    if (!combo) return; // bare modifier held — keep listening
    const clash = registeredKeybindings().find(
      (other) => other.id !== b.id && effectiveCombo(other) === combo && scopesOverlap(other.scope, b.scope),
    );
    if (clash) {
      setConflict({ id: b.id, with: clash.label });
      return; // keep recording so they can pick another
    }
    setBinding(b.id, combo);
    stopRecording();
  }

  const overrideCount = Object.keys(overrides).length;

  return (
    <div className="kb-panel">
      <div className="kb-panel__head">
        <p className="muted kb-panel__hint">
          Click a shortcut to rebind it. Note: <Kbd>⌘T</Kbd>, <Kbd>⌘1–9</Kbd> and <Kbd>⌃Tab</Kbd> are
          reserved by the browser — they work in the desktop app; in a browser, rebind to a free combo.
        </p>
        <Button variant="ghost" size="sm" onClick={resetAll} disabled={overrideCount === 0}>
          Reset all
        </Button>
      </div>

      {groups.map((g) => (
        <div className="kb-group" key={g}>
          <div className="kb-group__label">{g}</div>
          {bindings
            .filter((b) => (b.group || "Other") === g)
            .map((b) => {
              const recording = recordingId === b.id;
              const overridden = b.id in overrides;
              return (
                <div className="kb-row" key={b.id}>
                  <div className="kb-row__label">
                    {b.label}
                    {b.scope ? <span className="kb-row__scope">{b.scope}</span> : null}
                  </div>
                  <div className="kb-row__keys">
                    <button
                      type="button"
                      className={`kb-key${recording ? " kb-key--recording" : ""}`}
                      onClick={() => (recording ? stopRecording() : startRecording(b.id))}
                      onKeyDown={recording ? (e) => onRecordKey(b, e) : undefined}
                      onBlur={recording ? stopRecording : undefined}
                    >
                      {recording ? "Press keys… (Esc to cancel)" : formatCombo(effectiveCombo(b))}
                    </button>
                    {overridden ? (
                      <button
                        type="button"
                        className="kb-reset"
                        title="Reset to default"
                        aria-label={`Reset ${b.label} to default`}
                        onClick={() => resetBinding(b.id)}
                      >
                        ↺
                      </button>
                    ) : null}
                  </div>
                  {conflict?.id === b.id ? (
                    <div className="kb-row__conflict" role="alert">
                      Already bound to “{conflict.with}” — pick another.
                    </div>
                  ) : null}
                </div>
              );
            })}
        </div>
      ))}
    </div>
  );
}
