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

const REFUSALS: Record<number, string> = {
  409: PRIVATE_STEER_WAITING_ON_PEER,
  400: PRIVATE_STEER_TEXT_ONLY,
  403: PRIVATE_STEER_UNAVAILABLE,
};

/** The plain sentence for a refused PRIVATE steer, or null when `error` is not one of the
 *  server's refusals (a network failure, a 5xx) — the caller keeps its own wording for those.
 *  Only meaningful for a steer that was sent private: an ordinary chat's 400/403 means
 *  something else. */
export function privateSteerRefusal(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  return REFUSALS[error.status] ?? null;
}
