import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { localChatTurnsQuery } from "../lib/queries";

/** Settle old REST callers before a model choice takes effect. A failed stop must
 * keep the choice unchanged; the session lock remains authoritative throughout. */
export async function stopLocalTurns(sessionId: string) {
  const { turns } = await api.localChatTurns(sessionId);
  await Promise.all(turns.map((turn) => api.cancelLocalChatTurn(sessionId, turn.id)));
}

export function LocalTurnControls({ sessionId, visible }: { sessionId: string; visible: boolean }) {
  const turns = useQuery({ ...localChatTurnsQuery(sessionId), enabled: visible });
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  if (!turns.data?.turns.length) return null;
  return (
    <div className="composer-signed-out" role="status">
      <span>Chat request still running. Stop it to continue with another model.</span>
      <Button size="sm" variant="ghost" disabled={stopping} onClick={async () => {
        setStopping(true);
        setError("");
        try {
          await stopLocalTurns(sessionId);
          await turns.refetch();
        } catch (e) {
          setError(errMsg(e));
        } finally {
          setStopping(false);
        }
      }}>{stopping ? "Stopping…" : "Stop request"}</Button>
      {error ? <span role="alert">{error}</span> : null}
    </div>
  );
}
