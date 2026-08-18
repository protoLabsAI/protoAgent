"""TrajectoryMiddleware — record each model call's request envelope (ADR 0102 S1).

The writer half of the trajectory: at ``wrap_model_call`` — registered inside
ModelOverride (it must see the real per-tab model) but OUTSIDE PromptCache,
deliberately deviating from PromptCapture's after-cache placement: the refs
must hash the STORED message bytes so reconstruction joins against the
checkpoint, and PromptCache's rolling history marks (#2777) decorate a view-only
copy whose hashes would never match. The cache/wire decoration is a transport
concern already captured by wire_capture. It emits one ``request`` event
(stable-prefix hash, ordered message refs, bound-tools hash, model) and one
``response`` event (usage or the error class) per call. References only: message ids + content
hashes + sizes; the bytes stay in the checkpoint/archive (ADR 0102 D1/D2).

Best-effort: a trajectory failure never touches the call. Both emits are
synchronous appends of a few hundred bytes — noise next to a model round-trip.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)


class TrajectoryMiddleware(AgentMiddleware):
    def _emit_request(self, request) -> str:
        """Write the request event; returns the session key for the response pair."""
        from observability.trajectory import message_ref, sha_text, trajectory_log

        session = str(((getattr(request, "state", None) or {}).get("session_id")) or "")
        try:
            sysmsg = getattr(request, "system_message", None)
            messages = list(getattr(request, "messages", None) or [])
            tools = getattr(request, "tools", None) or []
            model = getattr(getattr(request, "model", None), "model_name", None) or getattr(
                getattr(request, "model", None), "model", ""
            )
            tool_names = sorted(str(getattr(t, "name", t)) for t in tools)
            trajectory_log.append(
                session,
                {
                    "t": "request",
                    "model": str(model or ""),
                    "stable_sha": sha_text(getattr(sysmsg, "content", "") or ""),
                    "tools_sha": sha_text("\n".join(tool_names)),
                    "tools_count": len(tool_names),
                    "msgs": [message_ref(m) for m in messages],
                },
            )
        except Exception:  # noqa: BLE001 — never touch the call
            log.debug("[trajectory] request emit failed", exc_info=True)
        return session

    @staticmethod
    def _usage_of(response) -> dict:
        try:
            for msg in getattr(response, "result", None) or []:
                um = getattr(msg, "usage_metadata", None)
                if isinstance(um, dict) and um:
                    details = um.get("input_token_details") or {}
                    return {
                        "input": int(um.get("input_tokens", 0) or 0),
                        "output": int(um.get("output_tokens", 0) or 0),
                        "cache_read": int(details.get("cache_read") or 0),
                        "cache_creation": int(details.get("cache_creation") or 0),
                    }
        except Exception:  # noqa: BLE001
            pass
        return {}

    def wrap_model_call(self, request, handler):
        from observability.trajectory import trajectory_log

        session = self._emit_request(request)
        try:
            response = handler(request)
        except Exception as exc:
            trajectory_log.append(session, {"t": "response", "status": "error", "error": type(exc).__name__})
            raise
        trajectory_log.append(session, {"t": "response", "status": "ok", "usage": self._usage_of(response)})
        return response

    async def awrap_model_call(self, request, handler):
        from observability.trajectory import trajectory_log

        session = self._emit_request(request)
        try:
            response = await handler(request)
        except Exception as exc:
            trajectory_log.append(session, {"t": "response", "status": "error", "error": type(exc).__name__})
            raise
        trajectory_log.append(session, {"t": "response", "status": "ok", "usage": self._usage_of(response)})
        return response
