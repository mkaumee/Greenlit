/**
 * What the screen says while the agent works.
 *
 * The animation cannot be tested here — `web/tests` is pure logic with no
 * jsdom — so the part that can be is: which step is running, which are done,
 * and that none are invented. That last one matters most. A progress list that
 * shows a step the data cannot support is worse than no list, because it reads
 * as knowledge.
 */

import { describe, expect, it } from "vitest";

import { researchOf, stillWorking } from "../src/chat/research";
import type { Item, Negotiation, Supplier } from "../src/hooks/useProject";

const band = {
  low: { amount: 120, currency: "MYR" },
  high: { amount: 400, currency: "MYR" },
  source_urls: ["https://example.invalid/a", "https://example.invalid/b"],
};

const item = (over: Partial<Item> = {}): Item => ({
  id: "mirror",
  name: "wall mirror",
  status: "RESEARCHING",
  ...over,
});

const seller = (id: string, name: string): Supplier => ({ id, name });

const negotiation = (over: Partial<Negotiation> = {}): Negotiation => ({
  id: "mirror--ahseng",
  item_id: "mirror",
  supplier_id: "ahseng",
  state: "DRAFTED",
  ...over,
});

describe("while research is running", () => {
  it("shows one running step and nothing it cannot know yet", () => {
    const [row] = researchOf([item()], [], []);

    expect(row?.steps).toHaveLength(1);
    expect(row?.steps[0]?.state).toBe("running");
    expect(row?.running).toBe(true);
  });

  it("closes that step and reports the price once the band lands", () => {
    const [row] = researchOf([item({ reference_band: band })], [], []);

    expect(row?.steps[0]?.state).toBe("done");
    expect(row?.steps[0]?.detail).toContain("MYR 120");
    expect(row?.steps[0]?.detail).toContain("2 source");
  });

  it("names the sellers it found", () => {
    const [row] = researchOf(
      [item({ reference_band: band, supplier_ids: ["ahseng", "skyline"] })],
      [seller("ahseng", "Ah Seng Rentals"), seller("skyline", "Skyline Props")],
      [],
    );

    const sellers = row?.steps.find((s) => s.key === "sellers");
    expect(sellers?.label).toBe("Found 2 sellers");
    expect(sellers?.detail).toBe("Ah Seng Rentals, Skyline Props");
  });
});

describe("as the emails are written", () => {
  const priced = item({
    status: "NEGOTIATING",
    reference_band: band,
    supplier_ids: ["ahseng", "skyline"],
  });
  const sellers = [
    seller("ahseng", "Ah Seng Rentals"),
    seller("skyline", "Skyline Props"),
  ];

  it("adds a line per negotiation as each is opened", () => {
    // The loop opens one negotiation per supplier, so these really do arrive
    // one at a time rather than all at once.
    const first = researchOf(priced ? [priced] : [], sellers, [negotiation()]);
    const both = researchOf(
      [priced],
      sellers,
      [negotiation(), negotiation({ id: "mirror--skyline", supplier_id: "skyline" })],
    );

    expect(first[0]?.steps.filter((s) => s.key.startsWith("write:"))).toHaveLength(1);
    expect(both[0]?.steps.filter((s) => s.key.startsWith("write:"))).toHaveLength(2);
    expect(both[0]?.steps.at(-1)?.label).toBe("Writing to Skyline Props");
  });

  it("marks one done the moment its draft exists", () => {
    const [row] = researchOf(
      [priced],
      sellers,
      [negotiation({ draft_body: "Hi Ah Seng," })],
    );

    const written = row?.steps.find((s) => s.key.startsWith("write:"));
    expect(written?.state).toBe("done");
    expect(written?.label).toBe("Written to Ah Seng Rentals");
  });

  it("is finished when every draft is written", () => {
    const rows = researchOf(
      [priced],
      sellers,
      [
        negotiation({ draft_body: "one" }),
        negotiation({ id: "mirror--skyline", supplier_id: "skyline", draft_body: "two" }),
      ],
    );

    expect(stillWorking(rows)).toBe(false);
  });
});

describe("what it leaves out", () => {
  it("ignores props nobody confirmed", () => {
    // A DRAFT item is inert. Showing it as waiting would read as the agent
    // stuck on something it was never asked to do.
    expect(researchOf([item({ status: "DRAFT" })], [], [])).toEqual([]);
  });

  it("ignores props that were dropped", () => {
    expect(researchOf([item({ status: "ABANDONED" })], [], [])).toEqual([]);
  });
});
