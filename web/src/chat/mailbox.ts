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

/**
 * What the panel knows, which is not the same as what the record says.
 *
 * `cardFor` takes this rather than `MailboxDoc | null | undefined` because
 * that signature could not distinguish "not read yet" from "the read was
 * refused", and rendered both as the Connect button — telling a producer with
 * a working mailbox to connect one. Worse, every field on `MailboxDoc` is
 * optional, so `{ status: "loading" }` satisfies it structurally and the
 * compiler had nothing to say about the two being mixed up.
 *
 * Kept in step with `useMailbox`, which produces it.
 */
export type MailboxState =
  | { status: "loading" }
  | { status: "none" }
  | { status: "have"; record: MailboxDoc }
  | { status: "denied"; detail: string };

export interface MailboxCard {
  /** Drives the colour, and nothing else. */
  tone: "none" | "connected" | "broken" | "unknown";
  headline: string;
  detail: string;
  /** The button's label, or empty for the states where there is nothing
   * useful to press. */
  action: string;
}

export function cardFor(state: MailboxState): MailboxCard {
  switch (state.status) {
    case "loading":
      // No button. A Connect button that flashes up for half a second on every
      // load of a connected account is a lie, and people click it.
      return {
        tone: "unknown",
        headline: "Checking your mailbox…",
        detail: "",
        action: "",
      };

    case "none":
      return {
        tone: "none",
        headline: "No mailbox connected",
        detail:
          "The agent negotiates from your Gmail, as you. Until one is " +
          "connected it can read a script and price things, but it cannot " +
          "write to a seller.",
        action: "Connect Gmail",
      };

    case "denied":
      // The state that did not exist, and cost an evening. Firestore refused
      // the read, which almost always means the deployed security rules are
      // behind the code — `match /mailboxes/{uid}` is recent, and `make deploy`
      // does not push rules.
      return {
        tone: "broken",
        headline: "Could not read your mailbox",
        detail:
          "Firestore refused the read, so this card cannot say whether a " +
          "mailbox is connected. Usually the deployed security rules are " +
          "behind the code: `make deploy-rules` pushes them. " +
          state.detail,
        action: "",
      };

    case "have":
      return cardForRecord(state.record);
  }
}

function cardForRecord(record: MailboxDoc): MailboxCard {
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
