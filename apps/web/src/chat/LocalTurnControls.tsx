import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import { onConnectionChange, onTopic } from "../lib/events";
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
  const qc = useQueryClient();
  // Pushed, not polled (#5064): refresh when the server says this session's turns changed,
  // and on every (re)connect so a change made while the stream was down is not missed.
  useEffect(() => {
    if (!visible) return;
    const { queryKey } = localChatTurnsQuery(sessionId);
    const refresh = () => void qc.invalidateQueries({ queryKey });
    const offTopic = onTopic("chat.local_turns", (data) => {
      if (data?.session_id === sessionId) refresh();
    });
    let wasConnected: boolean | undefined;
    const offConnection = onConnectionChange((connected) => {
      if (connected && wasConnected === false) refresh();
      wasConnected = connected;
    });
    return () => {
      offTopic();
      offConnection();
    };
  }, [qc, sessionId, visible]);
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
