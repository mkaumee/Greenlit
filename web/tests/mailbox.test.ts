/**
 * The mailbox card's wording, which is the whole feature.
 *
 * Two failures this covers, both of which have actually happened.
 *
 * An expired mailbox is the failure this system is least able to show and most
 * likely to hit: `gmail.modify` is restricted, so the consent screen stays in
 * Testing, so refresh tokens die after seven days — shorter than a
 * negotiation. On screen that is indistinguishable from every supplier being
 * slow.
 *
 * And a refused read. `cardFor` used to take `MailboxDoc | null | undefined`,
 * where `undefined` meant both "not read yet" and "Firestore said no", and
 * both rendered the Connect button. A producer whose mailbox was connected,
 * whose token was in Secret Manager and whose record was in Firestore was
 * shown a button asking them to connect it — because the deployed rules were
 * behind the code and nothing on the screen could say so.
 */

import { describe, expect, it } from "vitest";

import { cardFor, type MailboxDoc } from "../src/chat/mailbox";

const have = (record: MailboxDoc) => cardFor({ status: "have", record });

describe("cardFor", () => {
  it("asks for a mailbox when there is none", () => {
    const card = cardFor({ status: "none" });

    expect(card.tone).toBe("none");
    expect(card.action).toContain("Connect");
  });

  it("offers nothing to press before the first snapshot arrives", () => {
    // Not cosmetic. A Connect button that flashes up on every load of a
    // connected account is a lie, and a producer who clicks it burns a consent
    // grant to reconnect a mailbox that was never disconnected.
    const card = cardFor({ status: "loading" });

    expect(card.action).toBe("");
    expect(card.tone).not.toBe("none");
  });

  it("says a refused read is a refused read", () => {
    // The bug. This state did not exist and rendered as "no mailbox".
    const card = cardFor({ status: "denied", detail: "Missing permissions." });

    expect(card.tone).toBe("broken");
    expect(card.headline).not.toContain("No mailbox");
    // Names the cause and the fix: `match /mailboxes/{uid}` is recent and
    // `make deploy` does not push rules.
    expect(card.detail).toContain("deploy-rules");
    expect(card.detail).toContain("Missing permissions.");
  });

  it("does not offer Connect when it does not know", () => {
    // Connecting cannot fix a rules problem, and pressing it would leave the
    // producer thinking they had tried everything.
    expect(cardFor({ status: "denied", detail: "" }).action).toBe("");
  });

  it("shows the address suppliers will actually see", () => {
    const card = have({ email: "producer@example.com", status: "CONNECTED" });

    expect(card.tone).toBe("connected");
    expect(card.headline).toBe("producer@example.com");
  });

  it("offers reconnecting while the mailbox still works", () => {
    // A producer about to demo wants to re-consent before the seven days run
    // out, and there is no other way to do it.
    expect(have({ email: "a@b.com", status: "CONNECTED" }).action).toBe("Reconnect");
  });

  it("explains an expiry rather than just reporting it", () => {
    const card = have({ email: "a@b.com", status: "EXPIRED" });

    expect(card.tone).toBe("broken");
    expect(card.detail).toContain("seven days");
    expect(card.detail).toContain("picks up where it");
  });

  it("distinguishes a withdrawal from an expiry", () => {
    // Different cause, different fix: an expiry is routine, a revocation means
    // somebody took access away on purpose.
    const revoked = have({ email: "a@b.com", status: "REVOKED" });
    const expired = have({ email: "a@b.com", status: "EXPIRED" });

    expect(revoked.tone).toBe("broken");
    expect(revoked.headline).not.toBe(expired.headline);
  });

  it("refuses to call an unrecognised status healthy", () => {
    // A status this build has never heard of is a deployment mismatch. The
    // dangerous answer is the green one, so the unknown case is broken.
    const card = have({ email: "a@b.com", status: "SOMETHING_NEW" });

    expect(card.tone).toBe("broken");
    expect(card.detail).toContain("SOMETHING_NEW");
  });

  it("never calls a state connected unless the record says so", () => {
    // The compiler could not catch this before: every field on MailboxDoc is
    // optional, so `{ status: "loading" }` satisfied it structurally and the
    // two types were silently interchangeable.
    const notConnected = [
      cardFor({ status: "loading" }),
      cardFor({ status: "none" }),
      cardFor({ status: "denied", detail: "" }),
    ];

    expect(notConnected.map((c) => c.tone)).not.toContain("connected");
  });
});
