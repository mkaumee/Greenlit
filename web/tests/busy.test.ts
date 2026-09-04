/**
 * What the screen says while the agent is working.
 *
 * The animation itself cannot be tested here — `web/tests` is pure logic, with
 * no jsdom and no testing-library — so the part that can be is: the words. And
 * the words are the load-bearing half. A spinner tells you something is
 * happening; "Reading kopitiam-nights.txt…" tells you it is doing the thing
 * you asked, which is what stops a producer reloading the page twenty seconds
 * into a script read.
 */

import { describe, expect, it } from "vitest";

import { busyDetail, busyLabel, isBusy, IDLE, type Busy } from "../src/chat/busy";

describe("what is in flight", () => {
  it("says nothing at all when idle", () => {
    expect(busyLabel(IDLE)).toBe("");
    expect(busyDetail(IDLE)).toBe("");
    expect(isBusy(IDLE)).toBe(false);
  });

  it("names the file being read, because that is the long wait", () => {
    const reading: Busy = { kind: "reading", filename: "kopitiam-nights.txt" };

    expect(busyLabel(reading)).toBe("Reading kopitiam-nights.txt…");
    expect(isBusy(reading)).toBe(true);
  });

  it("still says something sensible with no filename", () => {
    expect(busyLabel({ kind: "reading", filename: "" })).toBe(
      "Reading the screenplay…",
    );
  });

  it("distinguishes a question from a screenplay", () => {
    // Two seconds and thirty seconds. One spinner saying the same thing for
    // both is how a working system reads as a hung one.
    expect(busyLabel({ kind: "asking" })).not.toBe(
      busyLabel({ kind: "reading", filename: "x.txt" }),
    );
  });

  it("only warns about a wait on the wait that is long", () => {
    expect(busyDetail({ kind: "asking" })).toBe("");
    expect(busyDetail({ kind: "reading", filename: "x.pdf" })).not.toBe("");
  });
});
