/**
 * The mailbox card's wording, which is the whole feature.
 *
 * An expired mailbox is the failure this system is least able to show and
 * most likely to hit: `gmail.modify` is restricted, so the consent screen
 * stays in Testing, so refresh tokens die after seven days — shorter than a
 * negotiation. On screen that is indistinguishable from every supplier being
 * slow, which is why each state has to say something different and why it is
 * worth asserting rather than eyeballing.
 */

import { describe, expect, it } from "vitest";

import { cardFor } from "../src/chat/mailbox";

describe("cardFor", () => {
  it("asks for a mailbox when there is none", () => {
    const card = cardFor(null);

    expect(card.tone).toBe("none");
    expect(card.action).toContain("Connect");
  });

  it("does not decide anything before the first snapshot arrives", () => {
    // `undefined` is 'not read yet'. It renders the same card as 'none' by
    // design — the alternative is a blank rail — but the caller is what keeps
    // them apart, so this only pins that undefined does not throw.
    expect(cardFor(undefined).tone).toBe("none");
  });

  it("shows the address suppliers will actually see", () => {
    const card = cardFor({ email: "producer@example.com", status: "CONNECTED" });

    expect(card.tone).toBe("connected");
    expect(card.headline).toBe("producer@example.com");
  });

  it("offers reconnecting while the mailbox still works", () => {
    // Not decoration: a producer about to demo wants to re-consent before the
    // seven days run out, and there is no other way to do it.
    expect(cardFor({ email: "a@b.com", status: "CONNECTED" }).action).toBe(
      "Reconnect",
    );
  });

  it("explains an expiry rather than just reporting it", () => {
    const card = cardFor({ email: "a@b.com", status: "EXPIRED" });

    expect(card.tone).toBe("broken");
    expect(card.detail).toContain("seven days");
    expect(card.detail).toContain("picks up where it");
  });

  it("distinguishes a withdrawal from an expiry", () => {
    // Different cause, different fix: an expiry is routine and a revocation
    // means somebody took access away on purpose.
    const revoked = cardFor({ email: "a@b.com", status: "REVOKED" });
    const expired = cardFor({ email: "a@b.com", status: "EXPIRED" });

    expect(revoked.tone).toBe("broken");
    expect(revoked.headline).not.toBe(expired.headline);
  });

  it("refuses to call an unrecognised status healthy", () => {
    // A status this build has never heard of is a deployment mismatch. The
    // dangerous answer is the green one, so the unknown case is broken.
    const card = cardFor({ email: "a@b.com", status: "SOMETHING_NEW" });

    expect(card.tone).toBe("broken");
    expect(card.detail).toContain("SOMETHING_NEW");
  });
});
