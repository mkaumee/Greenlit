/**
 * Connect the Gmail the agent negotiates from.
 *
 * The button opens Google's consent screen in a popup and then does nothing
 * else. It does not wait for the popup, and it does not poll: the callback
 * lands on `cinema-api`, which writes `mailboxes/{uid}` and closes the window,
 * and the Firestore snapshot behind `useMailbox()` is what flips this card. A
 * page that waited on the popup would break the moment somebody completed
 * consent on their phone, which is a thing people do.
 *
 * Reconnect is the same button. That is not laziness — the seven-day token
 * expiry means this is pressed roughly weekly for the life of the project, and
 * a separate "repair" flow would be a second path to the same place.
 */

import { useState } from "react";

import { startConsent } from "@/chat/api";
import { cardFor, type MailboxState } from "@/chat/mailbox";

const TONES: Record<string, string> = {
  none: "border-dashed",
  connected: "",
  broken: "border-destructive/50",
  unknown: "border-dashed opacity-60",
};

export function MailboxCard({ state }: { state: MailboxState }) {
  const card = cardFor(state);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const connect = () => {
    setError("");
    setBusy(true);
    void startConsent()
      .then((consent) => {
        if (consent.kind === "error") {
          setError(consent.detail);
          return;
        }
        // Named, so a second press reuses the same window rather than
        // stacking consent screens a producer then has to close one by one.
        const opened = window.open(consent.authorizeUrl, "greenlit-consent");
        if (opened === null) {
          setError(
            "Your browser blocked the popup. Allow popups for this site and " +
              "press it again.",
          );
        }
      })
      .finally(() => setBusy(false));
  };

  return (
    <div className={`rounded-lg border p-3 text-sm ${TONES[card.tone] ?? ""}`}>
      <p className="font-medium break-all">{card.headline}</p>
      <p className="mt-1 text-xs text-muted-foreground">{card.detail}</p>
      {card.action !== "" && (
        <button
          type="button"
          disabled={busy}
          onClick={connect}
          className="mt-2 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
        >
          {busy ? "Opening Google…" : card.action}
        </button>
      )}
      {error !== "" && <p className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  );
}
