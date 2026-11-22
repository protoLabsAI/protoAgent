import { ApiError } from "../lib/api/http";

// A steer into a conversation a peer started (From others) is always private (#5400): the
// operator's words stay between them and their agent. The server never downgrades one to an
// ordinary, peer-readable steer — it refuses instead — and these are the sentences the
// operator reads for each refusal. One home for them, so every surface that steers such a
// conversation (the chat composer, Work's waiting-task reply) says the same thing and none
// shows a status code or the server's raw detail.

/** 409 — the thread is parked on a question the peer itself asked. */
export const PRIVATE_STEER_WAITING_ON_PEER =
  "Not sent: this conversation is waiting on the other agent's own question. Your message is still in the composer.";
/** 400 — a private message is text only (no images, no incognito). */
export const PRIVATE_STEER_TEXT_ONLY =
  "Not sent: a private message to this conversation can only be text. Remove the images and send again.";
/** 403 — private steering is off, or the caller is not a signed-in operator. */
export const PRIVATE_STEER_UNAVAILABLE =
  "Not sent: private messages to conversations others started are turned off or need you to sign in again.";

/** Delivery may be uncertain after a transport failure; do not claim it was never sent. */
export const PRIVATE_STEER_DELIVERY_FAILED =
  "Couldn't confirm delivery of your private message; your text is still in the composer.";

const REFUSALS: Record<number, string> = {
  409: PRIVATE_STEER_WAITING_ON_PEER,
  400: PRIVATE_STEER_TEXT_ONLY,
  403: PRIVATE_STEER_UNAVAILABLE,
  503: PRIVATE_STEER_DELIVERY_FAILED,
};

// The server's own fixed refusal details on its private branch (src/operator_api/chat_routes.py
// `_private_steer`, src/server/chat.py's parked-decision 409). They are what tells a private
// refusal apart when the console did NOT ask for private: the server takes the private path
// for any peer-started conversation on its own (#5401), including one the Conversations list
// has not loaded (outside its window), and its refusal must still read as a sentence and hand
// the words back. Matched as fragments so a reworded tail does not break the match.
const SERVER_REFUSAL_DETAILS: ReadonlyArray<readonly [number, string]> = [
  [409, "a private steer can't answer"],
  [400, "A private steer carries text only"],
  [403, "Private steering is not available"],
  [503, "The private steer could not be delivered"],
];

// The fleet proxy's own 409 for a slug-routed call whose member is down (`isAgentNotRunning`,
// graph/fleet/proxy.py: "agent 'x' is not running"). It is not the thread waiting on a peer.
const AGENT_NOT_RUNNING = /\bis not running\b/;

/** A plain sentence for every failed private send, so callers always hand the text back.
 *  Fixed server details also identify private refusals for sessions absent from the loaded
 *  Conversations list. Other ordinary-chat failures stay with the caller's existing path. */
export function privateSteerRefusal(error: unknown, sentPrivate = true): string | null {
  const fallback = sentPrivate ? PRIVATE_STEER_DELIVERY_FAILED : null;
  if (!(error instanceof ApiError)) return fallback;
  const detail = error.message ?? "";
  for (const [status, fragment] of SERVER_REFUSAL_DETAILS) {
    if (error.status === status && detail.includes(fragment)) return REFUSALS[status];
  }
  if (!sentPrivate) return null;
  if (error.status === 409 && AGENT_NOT_RUNNING.test(detail)) return fallback;
  return REFUSALS[error.status] ?? fallback;
}
