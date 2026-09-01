/**
 * Whether the agent can send as this producer, and what to say about it.
 *
 * Kept apart from the card that renders it because the interesting part is
 * pure and the failure is silent. A mailbox that has quietly expired looks,
 * from the screen, exactly like a production where every supplier is slow:
 * nothing moves and nothing says why. Getting the wording right for each state
 * is most of the value here, so each state is a value a test can assert on
 * rather than a branch buried in JSX.
 *
 * The seven-day expiry is not an edge case. `gmail.modify` is a restricted
 * scope, so the consent screen stays in Testing, so refresh tokens die after
 * seven days — shorter than a negotiation. A working system spends part of
 * every week in EXPIRED, and this card is the only thing that says so.
 *
 * Nothing is imported here, deliberately: `startConsent` lives in `api.ts`
 * with the other calls that need a signed-in user, so this file can be
 * tested without standing up Firebase.
 */

/** `mailboxes/{uid}`, as the browser is allowed to see it. Metadata only —
 * the refresh token lives in Secret Manager, never here. */
export interface MailboxDoc {
  email?: string;
  status?: string;
}

export interface MailboxCard {
  /** Drives the colour, and nothing else. */
  tone: "none" | "connected" | "broken";
  headline: string;
  detail: string;
  /** The button's label. Never empty: every state has something to do, even
   * the healthy one, because a producer about to demo wants to re-consent
   * before the seven days run out rather than after. */
  action: string;
}

export function cardFor(record: MailboxDoc | null | undefined): MailboxCard {
  if (!record) {
    return {
      tone: "none",
      headline: "No mailbox connected",
      detail:
        "The agent negotiates from your Gmail, as you. Until one is " +
        "connected it can read a script and price things, but it cannot " +
        "write to a seller.",
      action: "Connect Gmail",
    };
  }

  const address = record.email ?? "";
  switch (record.status) {
    case "CONNECTED":
      return {
        tone: "connected",
        headline: address || "Mailbox connected",
        detail: "Suppliers see this address. Replies come back to it.",
        action: "Reconnect",
      };
    case "EXPIRED":
      return {
        tone: "broken",
        headline: "Reconnect your mailbox",
        detail:
          "Google's consent screen is in testing, so it issues refresh " +
          "tokens that expire after seven days — shorter than a " +
          "negotiation. Nothing is lost; the agent picks up where it " +
          "stopped on the next tick.",
        action: "Reconnect Gmail",
      };
    case "REVOKED":
      return {
        tone: "broken",
        headline: "Access was withdrawn",
        detail:
          "This mailbox was disconnected, either here or from your Google " +
          "account. The agent has stopped sending as you.",
        action: "Connect Gmail",
      };
    default:
      // An unrecognised status is a deployment mismatch, not a mailbox that
      // works. Saying so beats rendering it green on the strength of a string
      // this build has never heard of.
      return {
        tone: "broken",
        headline: "Mailbox state unknown",
        detail:
          `The record says "${record.status ?? ""}", which this build does ` +
          "not recognise. Reconnecting is safe and will settle it.",
        action: "Reconnect Gmail",
      };
  }
}
