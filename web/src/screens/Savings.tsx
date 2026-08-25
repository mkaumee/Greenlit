/**
 * What the negotiating was worth. Measured, not claimed.
 *
 * Every figure here is the difference between what a seller first asked and
 * what was actually accepted, summed. Nothing is estimated and nothing is
 * projected — if there is no accepted price yet, the item is not counted.
 */

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Item, Negotiation } from "@/hooks/useProject";
import { money, saving } from "@/lib/format";

export function Savings({
  items,
  negotiations,
}: {
  items: Item[];
  negotiations: Negotiation[];
}) {
  const rows = items
    .map((item) => {
      const best = negotiations
        .filter((n) => n.item_id === item.id && n.latest_quote !== undefined)
        .sort(
          (a, b) =>
            (a.latest_quote?.unit_price?.amount ?? Infinity) -
            (b.latest_quote?.unit_price?.amount ?? Infinity),
        )[0];
      if (best === undefined) return null;
      return { item, negotiation: best, won: saving(best.first_quote, best.latest_quote) };
    })
    .filter((r) => r !== null);

  const currency = rows[0]?.won?.amount.currency ?? "MYR";
  const total = rows.reduce((sum, r) => sum + (r.won?.amount.amount ?? 0), 0);
  const opened = rows.reduce(
    (sum, r) => sum + (r.negotiation.first_quote?.unit_price?.amount ?? 0),
    0,
  );

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Talked down by {money({ amount: total, currency })}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {total > 0 ? (
            <p>
              Sellers opened at {money({ amount: opened, currency })} across{" "}
              {rows.length} item{rows.length === 1 ? "" : "s"}. Every figure is a
              real first offer against a real current one — nothing projected.
            </p>
          ) : (
            <p>Nothing has moved off its opening price yet.</p>
          )}
        </CardContent>
      </Card>

      <div className="space-y-2">
        {rows.map(({ item, negotiation, won }) => (
          <div
            key={item.id}
            className="flex items-center justify-between rounded-lg border px-4 py-3"
          >
            <span className="font-medium capitalize">{item.name ?? item.id}</span>
            <span className="text-sm">
              <span className="text-muted-foreground line-through">
                {money(negotiation.first_quote?.unit_price)}
              </span>{" "}
              → {money(negotiation.latest_quote?.unit_price)}
              {won !== null && (
                <span className="ml-2 text-muted-foreground">
                  (−{String(won.percent)}%)
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
