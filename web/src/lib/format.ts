/**
 * Formatting the producer sees. Two rules, both load-bearing.
 *
 * Money is never a formatted string in the database — it is an amount and a
 * currency, and it becomes text here and nowhere else. That is what stops a
 * mixed-currency total being faked by string concatenation upstream.
 *
 * Timestamps are simulated time, from the project's clock, and are labelled as
 * such wherever they appear. A screen that quietly shows real time next to a
 * five-day negotiation is actively misleading about what happened.
 */

import type { Money, Quote } from "@/hooks/useProject";

export const money = (value: Money | undefined): string =>
  value === undefined
    ? "—"
    : `${value.currency} ${value.amount.toLocaleString()}`;

export const simTime = (value: { toDate: () => Date } | undefined): string =>
  value === undefined
    ? "—"
    : value.toDate().toISOString().slice(0, 16).replace("T", " ");

export const simDay = (value: { toDate: () => Date } | undefined): string =>
  value === undefined ? "—" : value.toDate().toISOString().slice(0, 10);

/** What the agent talked the price down by, as an amount and a percentage.
 *
 * Returns null rather than zero when there is nothing to compare, so a screen
 * can say "no movement yet" instead of claiming a saving of nothing. */
export function saving(
  first: Quote | undefined,
  latest: Quote | undefined,
): { amount: Money; percent: number } | null {
  const from = first?.unit_price;
  const to = latest?.unit_price;
  if (from === undefined || to === undefined) return null;
  if (from.currency !== to.currency) return null;
  const delta = from.amount - to.amount;
  if (delta <= 0) return null;
  return {
    amount: { amount: delta, currency: from.currency },
    percent: Math.round((delta / from.amount) * 100),
  };
}
