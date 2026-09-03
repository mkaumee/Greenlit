/**
 * One decision per prop.
 *
 * The agent writes to several suppliers about the same item, so three
 * negotiations can sit at READY_FOR_HUMAN for one cup. `purchase_orders` is
 * created with `create()` keyed by the *item*, so approving any one of them is
 * final and the rest become unapprovable — which makes listing them as three
 * separate decisions with three Approve buttons an invitation to spend more
 * than necessary on a screen that looks like it is offering a choice.
 */

import { describe, expect, it } from "vitest";

import { decisionsFor } from "../src/chat/decisions";
import type { Item, Negotiation } from "../src/hooks/useProject";

const item = (id: string, over: Partial<Item> = {}): Item => ({
  id,
  name: id,
  ...over,
});

const quote = (
  id: string,
  itemId: string,
  amount: number,
  over: Partial<Negotiation> = {},
): Negotiation => ({
  id,
  item_id: itemId,
  supplier_id: `s-${id}`,
  state: "READY_FOR_HUMAN",
  latest_quote: { unit_price: { amount, currency: "MYR" } },
  ...over,
});

describe("decisionsFor", () => {
  it("collapses rival quotes for one prop into one decision", () => {
    const out = decisionsFor(
      [item("cup")],
      [quote("a", "cup", 1439), quote("b", "cup", 719), quote("c", "cup", 900)],
    );

    expect(out).toHaveLength(1);
    expect(out[0]?.chosen.id).toBe("b");
    expect(out[0]?.rivals.map((r) => r.id)).toEqual(["c", "a"]);
  });

  it("counts props, not negotiations", () => {
    // The number the rail shows. Eight negotiations across four props is four
    // things a producer has to decide, and saying eight is simply wrong.
    const items = [item("cup"), item("mirror")];
    const negotiations = [
      quote("a", "cup", 719),
      quote("b", "cup", 1439),
      quote("c", "mirror", 719),
      quote("d", "mirror", 1439),
    ];

    expect(decisionsFor(items, negotiations)).toHaveLength(2);
  });

  it("ignores negotiations the agent has not stopped on", () => {
    // READY_FOR_HUMAN is the stop condition and the only thing that counts as
    // needing a person. A queue that also collects merely-interesting rows
    // trains people to ignore it.
    const out = decisionsFor(
      [item("cup")],
      [
        quote("a", "cup", 719, { state: "AWAITING_REPLY" }),
        quote("b", "cup", 900, { state: "DEAD" }),
      ],
    );

    expect(out).toEqual([]);
  });

  it("drops an item that is already ordered", () => {
    const out = decisionsFor(
      [item("cup", { status: "ORDERED" })],
      [quote("a", "cup", 719)],
    );

    expect(out).toEqual([]);
  });

  it("drops a negotiation pointing at an item this project does not have", () => {
    expect(decisionsFor([], [quote("a", "ghost", 719)])).toEqual([]);
  });

  it("puts a supplier who never named a price behind one who did", () => {
    // No quote must never win on price by being absent from the comparison.
    const out = decisionsFor(
      [item("cup")],
      [quote("silent", "cup", 0, { latest_quote: undefined }), quote("a", "cup", 900)],
    );

    expect(out[0]?.chosen.id).toBe("a");
  });

  it("is stable in prop order", () => {
    const out = decisionsFor(
      [item("mirror"), item("cup")],
      [quote("a", "mirror", 719), quote("b", "cup", 719)],
    );

    expect(out.map((d) => d.item.id)).toEqual(["cup", "mirror"]);
  });
});
