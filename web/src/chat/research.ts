/**
 * What the agent is doing between "confirmed" and "ready to send".
 *
 * Confirming the props used to be followed by silence. The tick researches
 * each item, finds sellers, opens a negotiation per seller and writes the
 * opening email — several ticks and a minute or more — and the screen showed
 * nothing until the drafts appeared. Working and stuck looked identical.
 *
 * Every step below is a transition that already exists in Firestore, and the
 * panel is already subscribed to all three collections. Nothing new is stored
 * and the tick is untouched; this is only the reading of it.
 *
 * The steps are genuinely incremental, which is the part worth knowing:
 * `_open_negotiations` creates one negotiation per supplier in a loop, so the
 * "writing to…" lines really do arrive one at a time rather than being a
 * staggered animation over a single write. What is *not* incremental is the
 * research itself — `research_item` is one call that returns the price band and
 * every supplier together, with the web search happening inside it. So there is
 * no honest "searching… reading result two…" ladder here, and inventing one
 * would be a progress bar measuring nothing.
 *
 * Pure and here rather than in the component, like `decisions.ts`: `web/tests`
 * has no jsdom, so logic inside a component is verified by looking at it.
 */

import type { Item, Negotiation, Supplier } from "@/hooks/useProject";

/** Confirmed and not yet finished with. DRAFT is inert; ABANDONED is dropped. */
const LIVE = new Set(["RESEARCHING", "SOURCING", "NEGOTIATING"]);

export type StepState = "done" | "running" | "waiting";

/**
 * Type aliases rather than interfaces, and not a style choice: these travel as
 * assistant-ui tool-call arguments, and an interface has no index signature so
 * it does not satisfy `ReadonlyJSONValue`. `Prop` and `PendingOpening` are
 * declared this way for the same reason.
 */
export type Step = {
  key: string;
  label: string;
  state: StepState;
  /** Extra detail under the label — a price band, the sellers found. */
  detail?: string;
};

export type ResearchItem = {
  itemId: string;
  name: string;
  steps: Step[];
  /** True while this item still has something in flight. */
  running: boolean;
};

/**
 * A band as one phrase: "MYR 120–400".
 *
 * The currency once, not twice. "MYR 120–MYR 400" is what you get from
 * formatting each end separately and it reads as two prices rather than a
 * range.
 */
const bandOf = (
  low: { amount: number; currency: string } | undefined,
  high: { amount: number; currency: string } | undefined,
): string => {
  if (!low || !high) return "";
  const range = `${low.amount.toLocaleString()}–${high.amount.toLocaleString()}`;
  return low.currency === high.currency
    ? `${low.currency} ${range}`
    : `${low.currency} ${low.amount.toLocaleString()}–${high.currency} ${high.amount.toLocaleString()}`;
};

/**
 * One entry per confirmed item, with its steps in order.
 *
 * Items that have not been confirmed are absent entirely rather than shown as
 * waiting: nothing is going to happen to them, and a row that never moves reads
 * as the agent being stuck on it.
 */
/**
 * How many pages the price came from, or that it came from none.
 *
 * "no sources" said plainly rather than "0 sources" tucked at the end: a band
 * with nothing behind it is the model's guess, and the panel says so elsewhere
 * for the same reason.
 */
const sourceCount = (n: number): string =>
  n === 0 ? "no sources" : n === 1 ? "1 source" : `${String(n)} sources`;

export function researchOf(
  items: Item[],
  suppliers: Supplier[],
  negotiations: Negotiation[],
): ResearchItem[] {
  const nameOf = (id: string | undefined) =>
    suppliers.find((s) => s.id === id)?.name ?? id ?? "a seller";

  return items
    .filter((item) => LIVE.has(item.status ?? ""))
    .map((item) => {
      const mine = negotiations.filter((n) => n.item_id === item.id);
      const priced = item.reference_band !== undefined;
      const sellers = item.supplier_ids ?? [];

      const steps: Step[] = [
        {
          key: "research",
          label: priced
            ? "Looked up what it costs and who has one"
            : "Looking up what it costs and who has one",
          state: priced ? "done" : "running",
          detail: priced
            ? [
                bandOf(item.reference_band?.low, item.reference_band?.high),
                sourceCount(item.reference_band?.source_urls?.length ?? 0),
              ]
                .filter((part) => part !== "")
                .join(" · ")
            : undefined,
        },
      ];

      if (priced) {
        steps.push({
          key: "sellers",
          label:
            sellers.length === 1
              ? "Found 1 seller"
              : `Found ${String(sellers.length)} sellers`,
          state: sellers.length > 0 ? "done" : "waiting",
          detail: sellers.map(nameOf).join(", ") || undefined,
        });
      }

      // One line per negotiation, appearing as each is opened. This is the
      // "got the first, then the second" the loop actually performs.
      for (const negotiation of mine) {
        const written = (negotiation.draft_body ?? "") !== "";
        const sent = negotiation.state !== "DRAFTED";
        steps.push({
          key: `write:${negotiation.id}`,
          label: sent
            ? `Sent to ${nameOf(negotiation.supplier_id)}`
            : written
              ? `Written to ${nameOf(negotiation.supplier_id)}`
              : `Writing to ${nameOf(negotiation.supplier_id)}`,
          state: written || sent ? "done" : "running",
        });
      }

      if (priced && sellers.length > 0 && mine.length === 0) {
        steps.push({
          key: "opening",
          label: "Writing the first emails",
          state: "running",
        });
      }

      return {
        itemId: item.id,
        name: item.name ?? item.id,
        steps,
        running: steps.some((s) => s.state === "running"),
      };
    });
}

/** True while any confirmed item still has work in flight. */
export const stillWorking = (rows: ResearchItem[]): boolean =>
  rows.some((row) => row.running);
