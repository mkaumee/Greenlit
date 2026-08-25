/**
 * The panel is the debugging surface for the rest of the system, so its own
 * failures have to be legible. Every case below is an error this project
 * actually hit on the way to hosting — `auth/configuration-not-found` was the
 * one that mattered, and its raw message named neither the cause nor the fix.
 */

import { describe, expect, it } from "vitest";

import { explain } from "../src/authErrors";

/** What the Firebase SDK actually throws: an Error carrying a `code`. */
const firebaseError = (code: string): Error =>
  Object.assign(new Error(`Firebase: Error (${code}).`), { code });

describe("explaining auth errors", () => {
  it("says to press Get started when Auth was never switched on", () => {
    const shown = explain(firebaseError("auth/configuration-not-found"));

    expect(shown).toContain("Get started");
    // The code stays visible — it is the searchable part.
    expect(shown).toContain("auth/configuration-not-found");
  });

  it("distinguishes a disabled provider from an absent config", () => {
    // These two look alike and mean different things: one needs the Google
    // toggle, the other needs Authentication to exist at all. Conflating them
    // sends you to a screen that is not there yet.
    const absent = explain(firebaseError("auth/configuration-not-found"));
    const disabled = explain(firebaseError("auth/operation-not-allowed"));

    expect(disabled).toContain("Sign-in method");
    expect(disabled).not.toBe(absent);
  });

  it("does not call a blocked popup a misconfiguration", () => {
    expect(explain(firebaseError("auth/popup-blocked"))).toContain(
      "nothing is misconfigured",
    );
  });

  it("falls through to the raw message for anything unrecognised", () => {
    // Better an unhelpful truth than a confident wrong explanation.
    const shown = explain(firebaseError("auth/some-future-code"));

    expect(shown).toBe("Firebase: Error (auth/some-future-code).");
  });

  it("survives being handed something that is not an Error", () => {
    expect(explain("plain string")).toBe("plain string");
    expect(explain(null)).toBe("null");
  });
});
