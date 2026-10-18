import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { subscribePaired, takePairedNotice } from "../lib/pairing";

/** "Paired as <name>" on the PHONE (#4336 S3). The operator's screen learns of a pairing from
 *  its device list growing; the phone used to just open the console with no word that it had
 *  been given its own token, or what it's called in that list.
 *
 *  Covers both claim paths: a `#pair=` link is redeemed BEFORE React mounts (the notice is
 *  waiting on first effect), and the AuthGate's typed code claims after (the subscription).
 *  `takePairedNotice` consumes, so StrictMode's double effect can't toast twice. */
export function PairingConfirmation() {
  const toast = useToast();
  useEffect(() => {
    const show = () => {
      const n = takePairedNotice();
      if (!n) return;
      toast({
        tone: "success",
        title: `Paired as ${n.name}`,
        message: "This device has its own token. Rename or remove it in Settings ▸ Devices.",
        duration: 10_000,
      });
    };
    show();
    return subscribePaired(show);
  }, [toast]);
  return null;
}
