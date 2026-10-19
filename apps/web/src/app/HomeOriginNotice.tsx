import { Banner } from "@protolabsai/ui/primitives";
import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { otherDeviceAddresses } from "../lib/credentials";
import { addressHost, addressLabel, consoleUrl, probeOrigin, requirementText } from "../lib/deviceHandoff";

import type { DeviceAddress } from "../lib/credentials";

/** How often a paired phone checks its home origin still answers, and how many misses in a
 *  row count as "unreachable" (one blip on a mobile network isn't). */
export const HOME_CHECK_MS = 15_000;
export const HOME_MISSES = 2;

/** "Can't reach this agent here" for a paired PHONE (ADR 0121 D1, #4336 S7.5).
 *
 *  A phone has one home origin. When it stops answering — the phone left the Wi-Fi a LAN
 *  pairing works on, say — this lists the agent's OTHER origins from the stored address set as
 *  plain links, each with what the phone needs for it, and says plainly that opening one means
 *  pairing again (a token never crosses origins). A `no-cors` probe of each is a hint only:
 *  "answering" when it resolves, nothing when it doesn't (an https page can't even probe an
 *  http origin). CORS is not widened for any of this.
 *
 *  Renders nothing for a browser that isn't a paired device, or that has nowhere else to go. */
export function HomeOriginNotice({ checkMs = HOME_CHECK_MS }: { checkMs?: number }) {
  const [others] = useState<DeviceAddress[]>(() => otherDeviceAddresses());
  const [down, setDown] = useState(false);
  const [answering, setAnswering] = useState<Record<string, boolean>>({});
  const watch = others.length > 0;

  useEffect(() => {
    if (!watch) return;
    let misses = 0;
    let stopped = false;
    const check = async () => {
      const up = await api.serverUp();
      if (stopped) return;
      misses = up ? 0 : misses + 1;
      setDown(misses >= HOME_MISSES);
    };
    const timer = window.setInterval(() => void check(), checkMs);
    // The OS saying the network changed is the moment to look again, not 15 s later.
    const now = () => void check();
    window.addEventListener("online", now);
    window.addEventListener("offline", now);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener("online", now);
      window.removeEventListener("offline", now);
    };
  }, [watch, checkMs]);

  useEffect(() => {
    if (!down) return;
    let stopped = false;
    for (const a of others) {
      void probeOrigin(a.origin).then((ok) => {
        if (!stopped && ok) setAnswering((m) => ({ ...m, [a.origin]: true }));
      });
    }
    return () => {
      stopped = true;
    };
  }, [down, others]);

  if (!watch || !down) return null;
  return (
    <Banner tone="warning" title="can't reach this agent here" className="shell-warning-banner home-origin-notice">
      <div data-testid="home-origin-notice">
        <p>This phone&apos;s address for the agent isn&apos;t answering. It may answer at another of its addresses:</p>
        <ul className="home-origin-list">
          {others.map((a) => (
            <li key={a.origin}>
              <a href={consoleUrl(a.origin)} rel="noreferrer">
                {addressLabel(a)} · {addressHost(a.origin)}
              </a>{" "}
              — {requirementText(a)}
              {answering[a.origin] ? <span className="home-origin-answering"> · answering</span> : null}
            </li>
          ))}
        </ul>
        <p>Opening one means pairing this phone again there. This address keeps working when you&apos;re back in range.</p>
      </div>
    </Banner>
  );
}
