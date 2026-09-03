/**
 * Whether a price band can be checked.
 *
 * `source_urls` has been written to Firestore on every researched item since
 * research first ran, and the panel's own ReferenceBand type declared only the
 * range — so the URLs arrived and were dropped. A band built from three
 * sources and one the model remembered rendered identically.
 *
 * These pin the two things that changes: sources are shown, and their absence
 * is shown too. The second is the one that matters, because an unsourced
 * number that looks like a sourced one is the failure this whole system is
 * built to avoid.
 */

import { describe, expect, it } from "vitest";

import { hostOf, sourceLabel } from "../src/lib/sources";

describe("sourceLabel", () => {
  it("says so when nothing backs the range", () => {
    // The tell for a number the model remembered rather than looked up.
    expect(sourceLabel([])).toBe("no sources");
    expect(sourceLabel(undefined)).toBe("no sources");
  });

  it("gets out of the way when there are sources to show", () => {
    expect(sourceLabel(["https://example.com/prices"])).toBeNull();
  });
});

describe("hostOf", () => {
  it("shows the publication, not the path", () => {
    expect(hostOf("https://www.shopee.com.my/mirror-prop-i.123.456?x=1")).toBe(
      "shopee.com.my",
    );
  });

  it("keeps a malformed source visible", () => {
    // Evidence about the model that produced it. Dropping it would make the
    // row look cleaner than the data is.
    expect(hostOf("not a url at all")).toBe("not a url at all");
  });

  it("does not collapse two different sources into one label", () => {
    expect(hostOf("https://a.example.com/x")).not.toBe(hostOf("https://b.example.com/x"));
  });
});
