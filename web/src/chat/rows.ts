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
  at: Date;
}

export type Row = ProducerRow | BriefingRow | ActivityRow | DecisionRow;

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
