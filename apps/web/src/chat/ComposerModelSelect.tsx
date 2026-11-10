import { useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { errMsg } from "../lib/format";
import { stopLocalTurns } from "./LocalTurnControls";

import { Badge } from "@protolabsai/ui/primitives";
import { Menu, MenuItem, MenuSeparator, type MenuHandle } from "@protolabsai/ui/menu";

import { runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { chatStore, useChatState } from "./chat-store";
import { bareModel, composerModelSections, groupByLane, lanesFromOptions, modelPickerData, sameModel } from "./modelForm";

// The composer's inline model picker — rendered in the DS PromptInput `actions` slot.
// This is a PER-TAB override: it does NOT change the saved global model (that lives in
// Settings). The choice is stored on the chat session and sent with each turn, so each
// tab can talk to its own model. Selecting the default-badged model clears the override
// → the configured global model. Available models come from the settings schema's
// `model.name` options (the gateway's live model list), the same source the wizard's
// picker uses — plus, when the operator is signed in to more than one lane, every
// lane's models (part B). Selecting a model from another lane switches THIS tab to that
// provider. Rows are plain model names grouped under a provider heading, because
// "anthropic-oauth:claude-sonnet-5" is slot syntax, not something to read in a menu.
export function ComposerModelSelect() {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreAfterSwitch = useRef(false);
  const menuRef = useRef<MenuHandle>(null);
  const touchTrigger = useRef(false);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState("");
  useEffect(() => {
    if (switching || !restoreAfterSwitch.current) return;
    restoreAfterSwitch.current = false;
    // Selection closes while the trigger is disabled during stopLocalTurns.
    // Finish Radix's focus return once enabled, unless the user moved elsewhere.
    if (document.activeElement === document.body) triggerRef.current?.focus({ preventScroll: true });
  }, [switching]);
  const schema = useQuery(settingsSchemaQuery());
  const runtime = useQuery(runtimeStatusQuery());
  const { sessions, currentSessionId } = useChatState();
  const field = schema.data?.groups.flatMap((g) => g.fields).find((f) => f.key === "model.name");

  const globalModel = String(field?.value ?? "");
  const picker = schema.data ? modelPickerData(schema.data.groups, runtime.data?.model?.provider ?? "") : null;
  const fallback = field?.options?.length ? field.options : globalModel ? [globalModel] : [];
  const sections = picker
    ? composerModelSections(picker)
    : { favorites: [], groups: groupByLane(fallback) };
  const options = [...sections.favorites, ...sections.groups.flatMap((group) => group.items)];
  const session = sessions.find((s) => s.id === currentSessionId);
  const selected = session?.model ?? "";

  // Under an ACP runtime the turn is driven by an external coding agent, not the
  // gateway model — so showing/picking a gateway model is misleading and inert.
  // Surface the active runtime as a static label instead of the model menu.
  const acpAgent = (runtime.data?.agent_runtime ?? "").startsWith("acp:")
    ? runtime.data!.agent_runtime!.slice("acp:".length)
    : "";
  if (acpAgent && currentSessionId) {
    return (
      <span className="composer-model-select" aria-label="Active runtime" title={`This chat runs on the ${acpAgent} coding agent (agent_runtime: acp:${acpAgent}) — not a gateway model.`}>
        {acpAgent}
        <Badge>coding agent</Badge>
      </span>
    );
  }

  if (!options.length || !currentSessionId) return null;

  const effectiveModel = selected || globalModel;
  const groups = sections.groups;
  // Same derivation the grouping used, so the badge and the headings agree.
  // From the SERVER-BUILT lists only: `options` may be the operator's favorites, which
  // can be stored bare, while the schema's `models`/`crossProvider` carry the qualified
  // names whose prefixes are registered ids by construction. The configured value itself
  // is deliberately NOT a source — it is whatever is stored, and inferring a connection
  // from it would disagree with the runtime.
  const knownLanes = lanesFromOptions([...(picker?.models ?? []), ...(picker?.crossProvider ?? []), ...options]);
  // Headings earn their place only when there's a choice of account to make.
  const showLanes = groups.length > 1;

  const item = (model: string) => {
    // Either side may be qualified or bare (ADR 0106 made the primary model name
    // its connection), so the comparison has to be spelling-agnostic in BOTH
    // directions — see sameModel.
    const isDefault = sameModel(model, globalModel, knownLanes);
    return (
      <MenuItem
        key={model}
        disabled={switching}
        onSelect={async () => {
          restoreAfterSwitch.current = true;
          setSwitching(true);
          setSwitchError("");
          try {
            await stopLocalTurns(currentSessionId);
            chatStore.setSessionModel(currentSessionId, isDefault ? "" : model);
          } catch (e) {
            setSwitchError(errMsg(e));
          } finally {
            setSwitching(false);
          }
        }}
      >
        {bareModel(model, knownLanes)}
        {isDefault ? <Badge>default</Badge> : null}
      </MenuItem>
    );
  };

  // The composer mounts once for the app's lifetime (visibility-toggled, never
  // remounted), so its boot-time schema fetch can race the server — graph still
  // compiling, provider probes empty — and the shared query then never refires
  // on its own (no refocus refetch, no interval). The menu would sit on the
  // one-model fallback until a Settings visit happened to refetch the cache.
  // Opening the menu is the moment freshness matters: refetch when the cached
  // schema is stale or came back without model options (the boot-race signature).
  const degraded = !!schema.data && !field?.options?.length;

  return (
    <>
    {switchError ? <span role="alert">{switchError}</span> : null}
    <Menu
      ref={menuRef}
      className="composer-model-menu"
      trigger={
        <button /* ds-audit-ignore hand-rolled-control — composite row/trigger, protoContent#551 */ type="button" className="composer-model-select" disabled={switching} aria-label="Model for this chat"
          ref={triggerRef}
          onPointerDownCapture={(event) => {
            touchTrigger.current = event.pointerType === "touch" || event.pointerType === "pen";
            // DS prevents pointerdown to defer touch opening to click. WebKit can
            // suppress that click entirely. Skip that handler without cancelling
            // the native gesture, so scrolling/cancelled taps still do not open.
            if (touchTrigger.current) event.stopPropagation();
          }}
          onPointerCancel={() => { touchTrigger.current = false; }}
          onKeyDownCapture={() => { touchTrigger.current = false; }}
          onClick={(event) => {
            if (!touchTrigger.current) return;
            touchTrigger.current = false;
            // Safari does not focus tapped buttons; establish the opener before
            // Radix takes focus so dismissal can return here instead of the editor.
            event.currentTarget.focus({ preventScroll: true });
            if (event.currentTarget.getAttribute("aria-expanded") === "true") menuRef.current?.close();
            else menuRef.current?.open();
          }}
        >
          {bareModel(effectiveModel, knownLanes)}
        </button>
      }
      align="start"
      onOpenChange={(open) => {
        if (open && (degraded || schema.isStale)) void schema.refetch();
      }}
    >
      {/* Grouped by account, with the provider as a section heading rather than a badge
          on every row: the rows are then just model names, which is what you're actually
          choosing between. A single-lane operator sees one unlabelled group — a lone
          "Gateway" heading over every row is chrome, not information. */}
      {sections.favorites.length ? (
        <>
          <div className="composer-model-lane" role="presentation">
            Favorites
          </div>
          {sections.favorites.map(item)}
          {groups.length ? <MenuSeparator /> : null}
        </>
      ) : null}
      {groups.map((group, gi) => (
        <Fragment key={group.lane || "_"}>
          {gi > 0 ? <MenuSeparator /> : null}
          {showLanes && group.label ? (
            <div className="composer-model-lane" role="presentation">
              {group.label}
            </div>
          ) : null}
          {group.items.map(item)}
        </Fragment>
      ))}
    </Menu>
    </>
  );
}
