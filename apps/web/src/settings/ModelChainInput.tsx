import { Combobox } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { Popover } from "@protolabsai/ui/overlays";
import { ChevronDown, ChevronUp, Plus, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import type { SettingsField } from "../lib/types";
import { moveListItem, settingListItems } from "./SettingsCategory";

// ADR 0133 M7b — the one chain control every model role uses. A role's chain is a list of
// models tried in order; a blank non-main role inherits its parent's chain (shown as one quiet
// "Inherits <parent>" line). The main chain (`model.name`) can never be emptied and folds any
// legacy `routing.fallback_models` in after its primary. See docs/design/model-chains-settings.md.

/** Split `provider:model` into its connection prefix and the model name, for the phone card's
 *  two wrapping lines (§2 item 11). An id with no `:` prefix (e.g. `protolabs/fast`) is all name. */
export function splitModelId(id: string): { prefix: string; name: string } {
  const idx = id.indexOf(":");
  if (idx === -1) return { prefix: "", name: id };
  return { prefix: id.slice(0, idx), name: id.slice(idx + 1) };
}

/** Fold legacy `routing.fallback_models` in after the main chain's primary, idempotently (ADR
 *  0133 §2 item 3): a model already in the chain is never duplicated, so once the next save has
 *  written the merged list into `model.name` the fold is a no-op and the value is stable. */
export function foldLegacyFallbacks(items: string[], legacy: readonly string[] | undefined): string[] {
  if (!legacy?.length) return items;
  return [...items, ...legacy.filter((model) => !items.includes(model))];
}

// True below the mobile breakpoint (mirrors ../lib/useIsMobile but guards `matchMedia`, which
// jsdom leaves undefined — so a settings test that mounts a chain without stubbing it still gets
// the desktop layout instead of throwing in the effect).
const MOBILE_QUERY = "(max-width: 767px)";
function useIsPhone(): boolean {
  const [phone, setPhone] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(MOBILE_QUERY);
    const on = () => setPhone(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return phone;
}

type PendingFocus = { row: number; dir: "up" | "down" | "input" } | null;

export function ModelChainInput({
  field,
  value,
  onChange,
  // The resolved chain of the parent role this one inherits (ADR 0133 §2 item 4) — what the
  // "Inherits <parent>" toggletip lists and what "Set its own chain" pre-fills. M7c resolves it
  // from the parent's current (including unsaved) value; absent → an empty chain.
  parentChain = [],
  // Legacy `routing.fallback_models`, folded into the main chain (§2 item 3). M7c passes it; the
  // generic SettingInput route leaves it undefined.
  legacyFallbacks,
}: {
  field: SettingsField;
  value: unknown;
  onChange: (value: unknown) => void;
  parentChain?: string[];
  legacyFallbacks?: readonly string[];
}) {
  // The main chain (`model.name`) is the only role with no parent: it can't be emptied and it
  // absorbs the legacy fallbacks. Every other role may be blanked back to inheriting.
  const parent = field.inherits ?? ""; // "" | "main" | "aux" | "harvest"
  const isMain = !parent;
  const groupLabel = `${field.label} model chain`;
  const options = field.options;

  const persisted = isMain
    ? foldLegacyFallbacks(settingListItems(value), legacyFallbacks)
    : settingListItems(value);

  // Local display rows may hold a just-added blank row (criterion 2) — display-only, never saved.
  const [rows, setRows] = useState<string[]>(persisted);
  // When a currently-empty role is switched to its own chain (the inherited chain was itself
  // empty, so there is nothing to pre-fill) we still open the editor with one blank row to type.
  const [editing, setEditing] = useState(false);
  // Phone only (§2 item 11): which row's full-width Combobox is open, and the value it rolls back
  // to on Cancel.
  const [phoneEdit, setPhoneEdit] = useState<{ row: number; original: string } | null>(null);
  const [live, setLive] = useState("");

  const lastEmitted = useRef<string[]>(persisted);
  const pendingFocus = useRef<PendingFocus>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const idBase = useId();
  const phone = useIsPhone();

  // Adopt an external change (Discard / Reset to inherited / a legacy fold) but never clobber the
  // in-progress blank row we just added — that keeps `rows` ahead of the saved value on purpose.
  const persistedKey = JSON.stringify(persisted);
  useEffect(() => {
    if (persistedKey !== JSON.stringify(lastEmitted.current)) {
      setRows(persisted);
      lastEmitted.current = persisted;
      if (persisted.length === 0) setEditing(false); // reverted to inheriting
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persistedKey]);

  useLayoutEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    pendingFocus.current = null;
    const row = rootRef.current?.querySelector<HTMLElement>(`[data-chain-row="${target.row}"]`);
    if (!row) return;
    if (target.dir === "input") {
      row.querySelector<HTMLElement>("input")?.focus();
      return;
    }
    // Keep focus on the moved row's button. If the move put the row on a boundary its same-
    // direction button is now disabled (unfocusable), so fall back to the row's other move
    // button, then its input — focus always lands somewhere on the row the operator moved.
    const other = target.dir === "up" ? "down" : "up";
    const candidates = [
      row.querySelector<HTMLButtonElement>(`[data-dir="${target.dir}"]`),
      row.querySelector<HTMLButtonElement>(`[data-dir="${other}"]`),
      row.querySelector<HTMLElement>("input"),
    ];
    for (const el of candidates) {
      if (el && !(el as HTMLButtonElement).disabled) {
        el.focus();
        return;
      }
    }
  }, [rows]);

  const emit = (next: string[]) => {
    const clean = next.filter((model) => model.trim() !== "");
    lastEmitted.current = clean;
    onChange(clean);
  };
  const update = (next: string[]) => {
    setRows(next);
    emit(next);
  };

  const setRowValue = (i: number, v: string) => update(rows.map((model, j) => (j === i ? v : model)));

  const move = (i: number, delta: number, dir: "up" | "down") => {
    const next = moveListItem(rows, i, delta);
    if (next === rows) return; // boundary no-op (the buttons are disabled there anyway)
    const target = i + delta;
    update(next);
    setLive(`${rows[i].trim() || "New model"} moved to position ${target + 1}`);
    pendingFocus.current = { row: target, dir };
  };

  const removeRow = (i: number) => {
    const next = rows.filter((_, j) => j !== i);
    update(next);
    setLive(`${rows[i].trim() || "New model"} removed`);
    if (!isMain && next.filter((model) => model.trim() !== "").length === 0) setEditing(false);
    if (phoneEdit?.row === i) setPhoneEdit(null);
  };

  const addFallback = () => {
    const next = [...rows, ""];
    setRows(next);
    setEditing(true);
    pendingFocus.current = { row: next.length - 1, dir: "input" };
  };

  // Non-main roles only: build / clear an own chain (§2 item 4).
  const setOwnChain = () => {
    const seed = parentChain.length ? parentChain : [""];
    setRows(seed);
    setEditing(true);
    emit(seed);
    pendingFocus.current = { row: 0, dir: "input" };
  };
  const useParentChain = () => {
    setRows([]);
    setEditing(false);
    emit([]);
  };

  const showChain = isMain || rows.some((model) => model.trim() !== "") || editing;

  // ── Inheriting: one quiet "Inherits <parent>" line (no model id in the row) ──────────────────
  if (!showChain) {
    return (
      <div
        ref={rootRef}
        id={`set-${field.key}`}
        role="group"
        aria-label={groupLabel}
        className="model-chain model-chain--inherited"
      >
        <InheritToggletip parent={parent} chain={parentChain} phone={phone} />
        <Button
          variant="ghost"
          size="sm"
          type="button"
          className="model-chain__set-own"
          onClick={setOwnChain}
        >
          Set its own chain
        </Button>
      </div>
    );
  }

  // ── A set chain: one row per model ───────────────────────────────────────────────────────────
  const total = rows.length;
  const removeDisabled = isMain && rows.length <= 1; // the main chain can't be emptied

  return (
    <div
      ref={rootRef}
      id={`set-${field.key}`}
      role="group"
      aria-label={groupLabel}
      className={`model-chain${phone ? " model-chain--phone" : ""}`}
    >
      {rows.map((item, i) => {
        const name = item.trim() || "New model";
        const positionLabel = i === 0 ? "Primary" : "then";
        const unknown = item.trim() !== "" && options.length > 0 && !options.includes(item);
        const editingPhoneRow = phone && phoneEdit?.row === i;
        const { prefix, name: modelName } = splitModelId(item);
        const combobox = (
          <Combobox
            className="setting-input model-chain__combobox"
            options={options}
            value={item}
            aria-label={`${field.label} model ${i + 1} of ${total}`}
            placeholder="type or pick a model"
            onValueChange={(v) => setRowValue(i, v)}
          />
        );
        const controls = (
          <div className="model-chain__controls">
            <Button
              variant="ghost"
              size="sm"
              type="button"
              data-dir="up"
              aria-label={`Move ${name} up`}
              disabled={i === 0}
              onClick={() => move(i, -1, "up")}
            >
              <ChevronUp size={14} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              type="button"
              data-dir="down"
              aria-label={`Move ${name} down`}
              disabled={i === total - 1}
              onClick={() => move(i, 1, "down")}
            >
              <ChevronDown size={14} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              type="button"
              className="model-chain__remove"
              aria-label={`Remove ${name}`}
              disabled={removeDisabled}
              onClick={() => removeRow(i)}
            >
              <X size={14} aria-hidden />
            </Button>
          </div>
        );
        return (
          <div className="model-chain__row" data-chain-row={i} key={i}>
            <span className="model-chain__position">{positionLabel}</span>
            {phone ? (
              // Phone card (§2 item 11): the id wraps across its own two lines, and the controls
              // live on their own line below it so they can never squeeze it. Change swaps the id
              // for a full-width Combobox with Cancel / Done.
              editingPhoneRow ? (
                <div className="model-chain__phone-edit">
                  {combobox}
                  <div className="model-chain__phone-edit-actions">
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      onClick={() => {
                        if (phoneEdit) setRowValue(i, phoneEdit.original);
                        setPhoneEdit(null);
                      }}
                    >
                      Cancel
                    </Button>
                    <Button variant="primary" size="sm" type="button" onClick={() => setPhoneEdit(null)}>
                      Done
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="model-chain__id-lines">
                    {prefix ? <span className="model-chain__id model-chain__id-prefix">{prefix}:</span> : null}
                    <span className="model-chain__id model-chain__id-name">{modelName || "—"}</span>
                  </div>
                  <div className="model-chain__controls">
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      onClick={() => setPhoneEdit({ row: i, original: item })}
                    >
                      Change
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      data-dir="up"
                      aria-label={`Move ${name} up`}
                      disabled={i === 0}
                      onClick={() => move(i, -1, "up")}
                    >
                      <ChevronUp size={16} aria-hidden />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      data-dir="down"
                      aria-label={`Move ${name} down`}
                      disabled={i === total - 1}
                      onClick={() => move(i, 1, "down")}
                    >
                      <ChevronDown size={16} aria-hidden />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      className="model-chain__remove"
                      aria-label={`Remove ${name}`}
                      disabled={removeDisabled}
                      onClick={() => removeRow(i)}
                    >
                      <X size={16} aria-hidden />
                    </Button>
                  </div>
                </>
              )
            ) : (
              <>
                {combobox}
                {controls}
              </>
            )}
            {unknown ? (
              <p className="model-chain__warning setting-desc" role="note">
                Not a model on any of your connections. It will be skipped until it is.
              </p>
            ) : null}
          </div>
        );
      })}
      <div className="model-chain__actions">
        <Button
          variant="ghost"
          size="sm"
          type="button"
          className="model-chain__add"
          onClick={addFallback}
        >
          <Plus size={14} aria-hidden /> Add fallback
        </Button>
        {!isMain ? (
          <Button variant="ghost" size="sm" type="button" className="model-chain__use-parent" onClick={useParentChain}>
            Use {parent}&rsquo;s chain
          </Button>
        ) : null}
      </div>
      {/* Reorder announcement (§3). Visually hidden — the console ships no sr-only utility. */}
      <div className="model-chain__live" role="status" aria-live="polite">
        {live}
      </div>
    </div>
  );
}

// The "Inherits <parent>" toggletip (ADR 0133 §2 item 4): a ghost button with a dotted underline
// that opens a Popover listing the full resolved parent chain. A Popover (not a Tooltip) so a
// phone can reach it by tap. The native <button> is the Radix trigger (it needs a ref, which the
// DS Button doesn't forward), styled as a ghost button; Radix toggles it on click / Enter / Space
// / tap and closes on Escape — we return focus to it explicitly so that holds in jsdom too.
function InheritToggletip({ parent, chain, phone }: { parent: string; chain: string[]; phone: boolean }) {
  const [open, setOpen] = useState(false);
  const popoverId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <Popover
      side="bottom"
      align="start"
      className={`model-chain__popover${phone ? " model-chain__popover--phone" : ""}`}
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) triggerRef.current?.focus();
      }}
      trigger={
        <button
          ref={triggerRef}
          type="button"
          className="pl-btn pl-btn--ghost pl-btn--sm model-chain__inherits"
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={popoverId}
        >
          Inherits {parent}
        </button>
      }
    >
      <div id={popoverId} className="model-chain__popover-body">
        <p className="model-chain__popover-title">Inherits {parent}&rsquo;s chain:</p>
        {chain.length ? (
          <ol className="model-chain__popover-list">
            {chain.map((model, i) => (
              <li key={i} className="model-chain__id">
                {model}
              </li>
            ))}
          </ol>
        ) : (
          <p className="model-chain__popover-empty">{parent} has no chain of its own yet.</p>
        )}
      </div>
    </Popover>
  );
}
