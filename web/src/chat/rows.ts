/**
 * What goes in the transcript.
 *
 * A chat library expects a conversation. This is mostly not one: two of the
 * four kinds below are the agent working — emails it sent, replies it got —
 * and they arrive from Firestore while nobody is typing. That is the thing
 * worth showing about a system that negotiates for days, so the transcript has
 * to hold them alongside the turns a person actually took.
 *
 * A discriminated union rather than one loose shape with optional fields,
 * because the converter switches on `kind` and TypeScript can then prove every
 * kind is handled. A row that grew a fifth kind and slipped through the
 * converter would render as nothing at all — silently, which is the failure
 * this project keeps having to design against.
 */

import type { Prop } from "./api";
import type { ResearchItem } from "./research";

/** Something an answer pointed at, that the panel renders as a link. */
export interface Reference {
  kind: string;
  id: string;
  label: string;
}

/** A turn the producer typed. */
export interface ProducerRow {
  kind: "producer";
  id: string;
  text: string;
  at: Date;
}

/** What came back from `/chat`. */
export interface BriefingRow {
  kind: "briefing";
  id: string;
  text: string;
  refs: Reference[];
  /** True when the deterministic summary answered instead of the brain.
   *
   * Shown, not hidden. Both answers are true, but only one reasoned, and a
   * deployment whose brain is misconfigured otherwise looks exactly like one
   * that is fine — which is the failure this project keeps designing against. */
  fromStoredFacts?: boolean;
  /** What went wrong, when the agent was asked and could not answer.
   *
   * On screen rather than only in Cloud Logging. "The agent did not answer
   * this one" with no reason attached is a diagnosis that costs a round trip
   * through the logs every single time, and the operator of a deployment is
   * the person reading this. */
  reason?: string;
  at: Date;
}

/**
 * One real email, in or out.
 *
 * Drawn from `projects/{pid}/negotiations/{nid}/messages`, which is append-only
 * — the timeline is the only proof that simulated days passed, and it stops
 * being evidence the moment anything can rewrite it.
 */
export interface ActivityRow {
  kind: "activity";
  id: string;
  negotiationId: string;
  direction: "outbound" | "inbound";
  supplier: string;
  itemName: string;
  subject: string;
  body: string;
  at: Date;
}

/** A negotiation the agent has stopped on, waiting for a person. */
export interface DecisionRow {
  kind: "decision";
  id: string;
  negotiationId: string;
  itemId: string;
  itemName: string;
  supplier: string;
  price: string;
  roundsUsed: number;
  reason: string;
  reasoning: string;
  /** How many other suppliers quoted for the same prop and were not chosen.
   * Worth saying: approving is keyed by item, so it settles all of them. */
  rivals: number;
  at: Date;
}

/**
 * Which way an email went.
 *
 * Case-insensitive because Firestore holds the contract enum's own spelling,
 * `INBOUND` / `OUTBOUND`, and an earlier version of this compared against
 * `"inbound"` — so every supplier reply in the transcript was labelled as
 * something the agent had sent. Nothing threw and nothing looked broken; the
 * transcript simply stopped being a record of a conversation and became a list
 * of outgoing mail. Exactly the failure this file's own comments warn about,
 * which is why the comparison is now a named function with a test rather than
 * an inline `===`.
 *
 * Anything unrecognised is outbound: an unlabelled message is far more likely
 * to be one of ours than a reply, and claiming a seller said something they
 * did not is the worse mistake.
 */
export const directionOf = (value: string | undefined): "inbound" | "outbound" =>
  value?.toLowerCase() === "inbound" ? "inbound" : "outbound";

/**
 * A screenplay that has been read, and the props it turned into.
 *
 * Lives in the transcript rather than in a modal because reading a script is
 * something the agent did, in order, alongside the emails it sent — and
 * because the confirmation gate is the most important thing on the screen. A
 * producer who scrolls past it has not confirmed anything, and nothing moves
 * until they do.
 */
export interface PropsRow {
  kind: "props";
  id: string;
  filename: string;
  props: Prop[];
  at: Date;
}

/**
 * One opening email, written and waiting to be read.
 *
 * A type alias rather than an interface, and not a style choice: an interface
 * has no index signature, so it does not satisfy assistant-ui's
 * `ReadonlyJSONValue` and cannot be carried as a tool-call argument. `Prop` is
 * declared this way for exactly the same reason.
 */
export type PendingOpening = {
  negotiationId: string;
  supplier: string;
  itemName: string;
  subject: string;
  body: string;
};

/**
 * The opening emails, held before first contact.
 *
 * One row for the whole batch rather than one per email, for the same reason
 * the props arrive as a single list: they are approved together, and a column
 * of separate Send buttons invites sending half of them.
 */
export interface OpeningsRow {
  kind: "openings";
  id: string;
  openings: PendingOpening[];
  at: Date;
}

/**
 * The agent working, between confirmation and the first drafts.
 *
 * One row for the whole production rather than one per prop: they are worked
 * on together across the same ticks, and a separate card per item would push
 * everything else out of the transcript.
 */
export interface ResearchRow {
  kind: "research";
  id: string;
  items: ResearchItem[];
  running: boolean;
  at: Date;
}

export type Row =
  | ProducerRow
  | BriefingRow
  | ActivityRow
  | DecisionRow
  | PropsRow
  | OpeningsRow
  | ResearchRow;

/**
 * Oldest first, by when each thing actually happened.
 *
 * Not by arrival: a Firestore snapshot can deliver a supplier's reply while a
 * `/chat` answer is still in flight, and appending in arrival order would put
 * yesterday's email after today's question. The timeline is evidence, so it
 * has to read in the order events occurred.
 *
 * Ties break on id so the order is stable across re-renders — two emails
 * written in the same simulated second must not swap places every snapshot.
 */
export const inOrder = (rows: Row[]): Row[] =>
  [...rows].sort((a, b) => {
    const byTime = a.at.getTime() - b.at.getTime();
    return byTime !== 0 ? byTime : a.id.localeCompare(b.id);
  });
