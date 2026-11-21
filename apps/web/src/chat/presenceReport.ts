import { api } from "../lib/api";

// Page-local identity; never restored from storage, even across reporter remounts.
const consoleId = Array.from(crypto.getRandomValues(new Uint32Array(4)), (n) => n.toString(16)).join("-");
const INPUT_MS = 60_000;
const inputEvents = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"] as const;
const sessionListeners = new Set<() => void>();

export function notifySessionChanged(): void {
  sessionListeners.forEach((notify) => notify());
}

/** Report positive evidence independently of the chat attendance stream. */
export function startPresenceReport(getSession: () => string | null): () => void {
  let lastInput: number | null = null;
  let lastSession: string | null = null;
  let active = false;
  let stopped = false;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let idle: ReturnType<typeof setTimeout> | undefined;

  function send(session: string, present: boolean) {
    void api.reportPresence({
      console_id: consoleId,
      session,
      present,
      input_age_s: lastInput === null ? 61 : Math.max(0, Date.now() - lastInput) / 1000,
    }).catch(() => { /* Best effort: the hub treats silent consoles as absent. */ });
  }

  function report() {
    if (stopped) return;
    const focused = document.visibilityState === "visible" && document.hasFocus();
    const session = getSession() || null;
    if (!focused) {
      if (active && lastSession) send(lastSession, false);
      active = false;
      clearInterval(heartbeat);
      heartbeat = undefined;
      clearTimeout(idle);
      return;
    }
    active = true;
    if (heartbeat === undefined) heartbeat = setInterval(report, 20_000);
    if (session) send(session, lastInput !== null && Date.now() - lastInput <= INPUT_MS);
    else if (lastSession) send(lastSession, false);
    lastSession = session;
    scheduleIdle();
  }

  function scheduleIdle() {
    clearTimeout(idle);
    if (active && lastInput !== null && Date.now() - lastInput <= INPUT_MS) {
      idle = setTimeout(report, INPUT_MS + 1 - (Date.now() - lastInput));
    }
  }

  function onInput() {
    const now = Date.now();
    const wasIdle = lastInput === null || now - lastInput > INPUT_MS;
    // High-frequency pointer/wheel events only update bookkeeping four times a second.
    if (!wasIdle && now - lastInput! < 250) return;
    lastInput = now;
    if (wasIdle) report();
    else scheduleIdle();
  }

  function onSessionChanged() {
    if (getSession() !== lastSession) report();
  }

  inputEvents.forEach((event) => window.addEventListener(event, onInput, { passive: true }));
  window.addEventListener("focus", report);
  window.addEventListener("blur", report);
  document.addEventListener("visibilitychange", report);
  sessionListeners.add(onSessionChanged);
  report();

  return () => {
    stopped = true;
    clearInterval(heartbeat);
    clearTimeout(idle);
    inputEvents.forEach((event) => window.removeEventListener(event, onInput));
    window.removeEventListener("focus", report);
    window.removeEventListener("blur", report);
    document.removeEventListener("visibilitychange", report);
    sessionListeners.delete(onSessionChanged);
  };
}
