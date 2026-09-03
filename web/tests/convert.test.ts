/**
 * The converter, which is where a mistake would be invisible.
 *
 * Nothing here throws when it is wrong. A row mapped to the wrong shape
 * renders as an empty bubble or as nothing at all, and the transcript quietly
 * stops being a record of what the agent did — so these assertions are the
 * only thing standing between a working screen and a convincing blank one.
 */

import { describe, expect, it } from "vitest";

import { DECISION_TOOL, EMAIL_TOOL, toThreadMessage } from "../src/chat/convert";
import { directionOf, inOrder, type Row } from "../src/chat/rows";

const AT = new Date("2026-03-01T09:00:00Z");

const decision = (over: Partial<Row> = {}): Row => ({
  kind: "decision",
  id: "d-1",
  negotiationId: "neg1",
  itemId: "mirror",
  itemName: "Mirror",
  supplier: "Ah Seng Rentals",
  price: "MYR 719",
  roundsUsed: 3,
  reason: "GOOD_QUOTE",
  reasoning: "They moved twice and have stopped moving.",
  at: AT,
  ...over,
} as Row);

const activity = (over: Partial<Row> = {}): Row => ({
  kind: "activity",
  id: "a-1",
  negotiationId: "neg1",
  direction: "inbound",
  supplier: "Ah Seng Rentals",
  itemName: "Mirror",
  subject: "Re: quote",
  body: "RM719 per unit.",
  at: AT,
  ...over,
} as Row);

const partsOf = (row: Row) => {
  const content = toThreadMessage(row).content;
  return typeof content === "string" ? [] : [...content];
};

describe("what a producer must decide", () => {
  it("carries an approval, so the library renders it as a decision", () => {
    const [part] = partsOf(decision());

    expect(part).toMatchObject({
      type: "tool-call",
      toolName: DECISION_TOOL,
      approval: { id: "neg1" },
    });
  });

  it("has no result, because it has not happened", () => {
    // A decision that looked completed would read as a purchase already made.
    // Nothing is bought until a person approves it, and the transcript must
    // not imply otherwise.
    const [part] = partsOf(decision());

    expect(part).not.toHaveProperty("result");
  });

  it("carries the evidence a producer needs to judge it", () => {
    // Price, who from, how many rounds, and the agent's own reasoning. Without
    // these the card is a yes/no prompt about a number the producer cannot
    // check — which is approval theatre, not approval.
    const [part] = partsOf(decision());

    expect(part).toMatchObject({
      args: {
        item: "Mirror",
        supplier: "Ah Seng Rentals",
        price: "MYR 719",
        rounds: 3,
        reasoning: "They moved twice and have stopped moving.",
      },
    });
  });
});

describe("what the agent already did", () => {
  it("is a completed tool call, not a pending one", () => {
    const [part] = partsOf(activity());

    expect(part).toMatchObject({ toolName: EMAIL_TOOL, result: { body: "RM719 per unit." } });
    expect(part).not.toHaveProperty("approval");
  });

  it("says which direction the email went", () => {
    // "We asked" and "they answered" look identical without it, and the whole
    // point of the feed is watching a conversation happen.
    const outbound = partsOf(activity({ direction: "outbound" } as Partial<Row>));

    expect(outbound[0]).toMatchObject({ args: { direction: "outbound" } });
  });
});

describe("turns a person took", () => {
  it("maps a producer row to a user message", () => {
    const message = toThreadMessage({
      kind: "producer",
      id: "p-1",
      text: "what needs me?",
      at: AT,
    });

    expect(message).toMatchObject({ role: "user" });
    expect(partsOf({ kind: "producer", id: "p-1", text: "what needs me?", at: AT })[0])
      .toMatchObject({ type: "text", text: "what needs me?" });
  });
});

describe("order", () => {
  it("reads in the order things happened, not the order they arrived", () => {
    // A Firestore snapshot can deliver yesterday's supplier reply while a
    // /chat answer is still in flight. Appending in arrival order would put
    // yesterday's email after today's question, and the timeline is evidence.
    const older = activity({ id: "a-old", at: new Date("2026-02-27T09:00:00Z") });
    const newer = activity({ id: "a-new", at: new Date("2026-03-02T09:00:00Z") });

    expect(inOrder([newer, older]).map((r) => r.id)).toEqual(["a-old", "a-new"]);
  });

  it("is stable when two things share a timestamp", () => {
    // Two emails in the same simulated second must not swap places on every
    // snapshot, which would make the transcript visibly flicker.
    const first = activity({ id: "a-1" });
    const second = activity({ id: "a-2" });

    expect(inOrder([second, first]).map((r) => r.id)).toEqual(["a-1", "a-2"]);
    expect(inOrder([first, second]).map((r) => r.id)).toEqual(["a-1", "a-2"]);
  });
});

describe("directionOf", () => {
  it("reads the spelling Firestore actually holds", () => {
    // The contract enum is INBOUND/OUTBOUND and that is what is stored. An
    // earlier version compared against "inbound", so every supplier reply in
    // the transcript was labelled as mail the agent had sent — silently, and
    // for as long as nobody read the screen.
    expect(directionOf("INBOUND")).toBe("inbound");
    expect(directionOf("OUTBOUND")).toBe("outbound");
  });

  it("still reads the lowercase form", () => {
    expect(directionOf("inbound")).toBe("inbound");
  });

  it("treats an unlabelled message as ours", () => {
    // Claiming a seller said something they did not is the worse mistake of
    // the two, so the unknown case is outbound.
    expect(directionOf(undefined)).toBe("outbound");
    expect(directionOf("")).toBe("outbound");
  });
});

describe("a briefing that did not come from the brain", () => {
  const briefing = (fromStoredFacts?: boolean, reason?: string): Row => ({
    kind: "briefing",
    id: "b-1",
    text: "Nothing needs you right now.",
    refs: [],
    fromStoredFacts,
    reason,
    at: AT,
  });

  const textOf = (row: Row): string => {
    const part = partsOf(row)[0];
    return part !== undefined && typeof part !== "string" && part.type === "text"
      ? part.text
      : "";
  };

  it("says so, rather than passing it off as the agent", () => {
    // Both answers are true; only one reasoned. A deployment whose brain is
    // misconfigured otherwise looks exactly like one that is fine, and
    // noticing that the prose feels flat is not a diagnosis.
    expect(textOf(briefing(true))).toContain("stored records");
  });

  it("leaves a real answer alone", () => {
    expect(textOf(briefing(false))).toBe("Nothing needs you right now.");
  });

  it("treats a missing flag as the agent having answered", () => {
    // Optional so an older row shape still renders, and the default has to be
    // the quiet one or every answer carries a disclaimer.
    expect(textOf(briefing())).not.toContain("stored records");
  });

  it("shows why the agent could not answer", () => {
    // The whole point. "The agent did not answer this one" with no reason is
    // a diagnosis that costs a trip through Cloud Logging every time, and the
    // person reading this screen is the one who deployed it.
    const text = textOf(briefing(true, "NotFound: Publisher Model not found"));

    expect(text).toContain("stored records");
    expect(text).toContain("NotFound: Publisher Model not found");
  });

  it("says nothing extra when there is no reason to give", () => {
    const text = textOf(briefing(true, ""));

    expect(text).toContain("stored records");
    expect(text.trimEnd().endsWith("answer this one.")).toBe(true);
  });

  it("never attaches a reason to an answer the agent gave", () => {
    // A reason on a working answer would be a disclaimer under every reply.
    expect(textOf(briefing(false, "NotFound: something"))).toBe(
      "Nothing needs you right now.",
    );
  });
});
