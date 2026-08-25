/**
 * Everything not waiting on you.
 *
 * The quiet half of the product. The agent spends most of its life here —
 * researching, emailing, waiting days for a reply — and the producer's job
 * during that time is to leave it alone. So this screen reports rather than
 * asks, and the one number that matters is how many items are still moving.
 */

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Item, Negotiation } from "@/hooks/useProject";
import { money, simTime } from "@/lib/format";

const TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  ORDERED: "default",
  READY_FOR_HUMAN: "default",
  NEGOTIATING: "secondary",
  SOURCING: "secondary",
  RESEARCHING: "secondary",
  DRAFT: "outline",
  ABANDONED: "destructive",
};

export function Breakdown({
  items,
  negotiations,
}: {
  items: Item[];
  negotiations: Negotiation[];
}) {
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-10 text-center">
        <p className="text-lg font-medium">No items yet.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Upload a screenplay and the agent reads it for props.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">
        {items.length} item{items.length === 1 ? "" : "s"} from the script
      </h2>
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Item</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Sellers</TableHead>
              <TableHead>Best so far</TableHead>
              <TableHead>Market</TableHead>
              <TableHead>Next action (sim)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => {
              const mine = negotiations.filter((n) => n.item_id === item.id);
              const best = mine
                .map((n) => n.latest_quote?.unit_price)
                .filter((p) => p !== undefined)
                .sort((a, b) => a.amount - b.amount)[0];
              const live = mine.filter(
                (n) => n.state !== "DEAD" && n.state !== "ORDERED",
              ).length;
              return (
                <TableRow key={item.id}>
                  <TableCell className="font-medium capitalize">
                    {item.name ?? item.id}
                    {item.consumable === true && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        ×{item.qty ?? 1}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant={TONE[item.status ?? ""] ?? "outline"}>
                      {item.status ?? "—"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {live}/{mine.length} live
                  </TableCell>
                  <TableCell>{money(best)}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {item.reference_band === undefined
                      ? "—"
                      : `${money(item.reference_band.low)}–${money(item.reference_band.high)}`}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {simTime(item.next_action_due_at)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
