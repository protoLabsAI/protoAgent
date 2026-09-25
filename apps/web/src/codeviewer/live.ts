import type { ComponentSpec, ToolEvent } from "../lib/types";
import { CODE_REF_COMPONENT, codeRefFromProps } from "./codeRef";
import { followRefFromTool } from "./followRef";
import { followCode, openCode } from "./open";

// The code pane's two hooks into the LIVE turn stream (ADR 0112). Called ONLY from
// ChatSurface's own stream handlers — never from reattach, boot hydration or the palette
// chat — because a component or tool call replayed from history is not the agent pointing at
// something NOW, and re-opening the pane on every reload would be the pane fighting the
// operator.

/** A live component-v1 part: a `code-ref` auto-opens the pane (desktop only — see openCode). */
export function onLiveComponent(spec: ComponentSpec): void {
  if (spec.component !== CODE_REF_COMPONENT) return;
  const ref = codeRefFromProps(spec.props);
  if (ref) openCode({ ...ref, source: "component" }, { auto: true });
}

/** A live tool frame: a COMPLETED fs call feeds follow mode. `input` is the call's args as the
 *  card holds them — the end frame itself may not repeat them. */
export function onLiveToolEvent(evt: ToolEvent, input: string | undefined): void {
  if (evt.phase !== "end" || evt.error) return;
  const ref = followRefFromTool(evt.name, input ?? evt.input, evt.output);
  if (ref) followCode(ref);
}
