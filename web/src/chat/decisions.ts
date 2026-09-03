/**
 * One decision per prop, not one per negotiation.
 *
 * The agent approaches several suppliers for the same item and can end up
 * holding two or three quotes that are all `READY_FOR_HUMAN`. Listing those as
 * separate decisions would be wrong twice over: it inflates the "needs you"
 * count against a producer who has four things to decide rather than eight,
 * and it puts an Approve button under the expensive quote sitting right beside
 * the cheap one. `purchase_orders` is keyed by item, so approving either is
 * final and the other becomes unapprovable — the screen must not make that
 * look like a choice between equals.
 *
 * Cheapest live quote wins and the rest become rivals, which is what the Inbox
 * has always done. This is that rule, lifted out so the rail, the transcript
 * and the Inbox cannot drift into three different counts of the same thing.
 *
 * Type-only imports here, deliberately: the shapes come from the hooks module
 * but nothing at runtime does, so this stays testable without Firebase.
 */

import type { Item, Negotiation } from "@/hooks/useProject";

export const WAITING = "READY_FOR_HUMAN";

export interface Decision {
  item: Item;
  /** The one an Approve button may act on. */
  chosen: Negotiation;
  /** Everyone else who quoted. Shown, never actionable. */
  rivals: Negotiation[];
}

const priceOf = (n: Negotiation): number =>
  n.latest_quote?.unit_price?.amount ?? Infinity;

export function decisionsFor(
  items: Item[],
  negotiations: Negotiation[],
): Decision[] {
  const byItem = new Map<string, Negotiation[]>();
  for (const n of negotiations) {
    if (n.state !== WAITING || n.item_id === undefined) continue;
    byItem.set(n.item_id, [...(byItem.get(n.item_id) ?? []), n]);
  }

  const out: Decision[] = [];
  for (const [itemId, group] of byItem) {
    const item = items.find((i) => i.id === itemId);
    // An item already ordered has nothing left to decide, and an item this
    // project does not have is a negotiation pointing at nothing — neither
    // belongs in a queue a person is asked to work through.
    if (item === undefined || item.status === "ORDERED") continue;
    const sorted = [...group].sort((a, b) => priceOf(a) - priceOf(b));
    const [chosen, ...rivals] = sorted;
    if (chosen === undefined) continue;
    out.push({ item, chosen, rivals });
  }
  return out.sort((a, b) => (a.item.name ?? "").localeCompare(b.item.name ?? ""));
}
