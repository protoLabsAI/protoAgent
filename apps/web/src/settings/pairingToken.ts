import { readKey, removeKeyStrict, writeKeyStrict } from "../lib/storage";

const TOKEN_KEY = "protoagent.authToken";

/** Carries a recovery credential only to the panel, never in the logged error message. */
export class PairingTokenRecoveryError extends Error {
  constructor(public readonly priorToken: string | null) {
    super("The server rejected the new token, and browser storage could not restore the previous credential. Free storage or enable it for this site before reconnecting.");
    this.name = "PairingTokenRecoveryError";
  }
}

/** Persist before changing live server auth; restore strictly after an explicit rejection. */
export async function savePairingToken(
  token: string,
  save: (token: string) => Promise<{ ok: boolean; messages: string[] }>,
): Promise<void> {
  const prior = readKey("local", TOKEN_KEY);
  writeKeyStrict("local", TOKEN_KEY, token);
  // A transport rejection has an unknown outcome: the server may have accepted the token.
  // Keep the new credential in that case; roll back only a confirmed server rejection.
  const res = await save(token);
  if (res.ok) return;
  try {
    if (prior !== null) writeKeyStrict("local", TOKEN_KEY, prior);
    else removeKeyStrict("local", TOKEN_KEY);
  } catch {
    throw new PairingTokenRecoveryError(prior);
  }
  throw new Error(res.messages.join(" · ") || "could not set an auth token");
}
