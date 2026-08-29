/**
 * The rules, executed.
 *
 * Until this file existed, `firestore.rules` and `firestore.orders.rules` had
 * never been run by anything. The Python suite proves `create()` refuses a
 * duplicate order, but it goes through the admin SDK, which bypasses rules
 * entirely — so every line in both files was unverified, and a typo in one
 * would have shipped invisibly.
 *
 * ## What this proves, and what it does not
 *
 * Rules govern the Firebase *client* SDKs: a producer's browser holding an Auth
 * token. They do not apply to the server SDK the agent uses. So this file is
 * not what stops the agent writing a purchase order — that is IAM, and the
 * separate `orders` database it has no binding on.
 *
 * What this file proves is the browser half: that an identity without the
 * `producer` claim is refused by *Firestore*, and that an order, once written,
 * cannot be rewritten by anyone at all — including the producer who made it.
 *
 * ## Why two test environments instead of two databases
 *
 * `@firebase/rules-unit-testing` loads one rules file per environment, and the
 * emulator keys rules by project ID. Two environments with different project
 * IDs therefore give two independent rules sets and two isolated data spaces,
 * which is exactly the shape of the deployment: two rulesets that cannot see
 * each other's documents.
 *
 * Both files are read explicitly by path rather than through `firebase.json`,
 * because firebase-tools does not load rules from the multi-database array
 * form — which is why the emulator otherwise runs wide open, and why relying on
 * it here would test nothing.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  assertFails,
  assertSucceeds,
  initializeTestEnvironment,
  type RulesTestEnvironment,
} from "@firebase/rules-unit-testing";
import {
  collection,
  collectionGroup,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  setDoc,
  setLogLevel,
  updateDoc,
  type Firestore,
} from "firebase/firestore";
import { afterAll, beforeAll, beforeEach, describe, it } from "vitest";

// Almost every assertion here is that a write is refused, and the SDK logs each
// refusal at error level. Left on, a passing run looks like a catastrophe.
setLogLevel("silent");

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const [HOST, PORT] = (
  process.env.FIRESTORE_EMULATOR_HOST ?? "127.0.0.1:8080"
).split(":");

/** A well-formed purchase order, as the Approve screen would write it. */
const order = (uid: string, itemId = "mirror") => ({
  item_id: itemId,
  project_id: "projA",
  supplier_id: "sup1",
  negotiation_id: "neg1",
  price: { amount: 880, currency: "MYR" },
  approved_by: uid,
  approved_at: new Date("2026-03-01T09:00:00Z"),
});

const PRODUCER = "producer-1";
const AGENT = "agent-service-account";

let orders: RulesTestEnvironment;
let main: RulesTestEnvironment;

beforeAll(async () => {
  const firestore = { host: HOST!, port: Number(PORT) };
  orders = await initializeTestEnvironment({
    projectId: "rules-orders",
    firestore: {
      ...firestore,
      rules: readFileSync(`${REPO}firestore.orders.rules`, "utf8"),
    },
  });
  main = await initializeTestEnvironment({
    projectId: "rules-default",
    firestore: {
      ...firestore,
      rules: readFileSync(`${REPO}firestore.rules`, "utf8"),
    },
  });
});

afterAll(async () => {
  await orders?.cleanup();
  await main?.cleanup();
});

beforeEach(async () => {
  await orders.clearFirestore();
  await main.clearFirestore();
});

/**
 * The one cast in this file, kept to a single place.
 *
 * `rules-unit-testing` declares `firestore()` as returning the *compat*
 * Firestore, while the API it documents — and the one it actually hands back —
 * is the modular one. Every example in its own docs passes the result straight
 * to modular `doc()`. Rather than loosen `tsconfig` for the whole project, the
 * mismatch is absorbed here and nowhere else.
 */
const db = (context: { firestore: () => unknown }): Firestore =>
  context.firestore() as Firestore;

/** A producer's browser. */
const asProducer = (env: RulesTestEnvironment): Firestore =>
  db(env.authenticatedContext(PRODUCER, { role: "producer" }));

/** Signed in, no claim — what the agent looks like to these rules. */
const asAgent = (env: RulesTestEnvironment): Firestore =>
  db(env.authenticatedContext(AGENT));

const asStranger = (env: RulesTestEnvironment): Firestore =>
  db(env.unauthenticatedContext());

/** Write past the rules, to set up a state the rules would not let us create. */
const seed = async (
  env: RulesTestEnvironment,
  path: string,
  data: object,
): Promise<void> => {
  await env.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(db(ctx), path), data);
  });
};

// --------------------------------------------------------------------------
// purchase_orders — who may spend money
// --------------------------------------------------------------------------

describe("purchase orders", () => {
  it("refuses an identity with no producer claim", async () => {
    // The headline. This is the agent's shape: authenticated, and still
    // refused — by Firestore, before a line of application code runs.
    await assertFails(
      setDoc(doc(asAgent(orders), "purchase_orders/mirror"), order(AGENT)),
    );
  });

  it("refuses a caller who is not signed in at all", async () => {
    await assertFails(
      setDoc(
        doc(asStranger(orders), "purchase_orders/mirror"),
        order("anyone"),
      ),
    );
  });

  it("lets a producer create one", async () => {
    await assertSucceeds(
      setDoc(
        doc(asProducer(orders), "purchase_orders/mirror"),
        order(PRODUCER),
      ),
    );
  });

  it("refuses an order signed with somebody else's uid", async () => {
    // Otherwise the audit trail is a field the client fills in freely, and
    // "who approved this" stops being an answer.
    await assertFails(
      setDoc(
        doc(asProducer(orders), "purchase_orders/mirror"),
        order("someone-else"),
      ),
    );
  });

  it("refuses an order whose body names a different item than its key", async () => {
    // The key is the guardrail — one order per item. A body claiming a
    // different item would make the two disagree about what was bought.
    await assertFails(
      setDoc(
        doc(asProducer(orders), "purchase_orders/mirror"),
        order(PRODUCER, "smoke-machine"),
      ),
    );
  });

  it("refuses a price that is a formatted string", async () => {
    // "RM880" is the money bug this codebase is built to make impossible, and
    // the storage layer is the last place it could still get in.
    await assertFails(
      setDoc(doc(asProducer(orders), "purchase_orders/mirror"), {
        ...order(PRODUCER),
        price: { amount: "880", currency: "MYR" },
      }),
    );
  });

  it("refuses a price of zero or less", async () => {
    await assertFails(
      setDoc(doc(asProducer(orders), "purchase_orders/mirror"), {
        ...order(PRODUCER),
        price: { amount: 0, currency: "MYR" },
      }),
    );
    await assertFails(
      setDoc(doc(asProducer(orders), "purchase_orders/mirror"), {
        ...order(PRODUCER),
        price: { amount: -880, currency: "MYR" },
      }),
    );
  });

  it("refuses a second order for an item already bought", async () => {
    // The same refusal whether it is a duplicate of the same deal or a second
    // supplier for the same prop, because the item is the key.
    await seed(orders, "purchase_orders/mirror", order(PRODUCER));

    await assertFails(
      setDoc(
        doc(asProducer(orders), "purchase_orders/mirror"),
        order(PRODUCER),
      ),
    );
  });

  it("refuses to let a producer rewrite their own order", async () => {
    // An order is a record of something that happened. Not even the person who
    // approved it gets to change the number afterwards.
    await seed(orders, "purchase_orders/mirror", order(PRODUCER));

    await assertFails(
      updateDoc(doc(asProducer(orders), "purchase_orders/mirror"), {
        price: { amount: 1, currency: "MYR" },
      }),
    );
  });

  it("refuses to let anyone delete an order", async () => {
    await seed(orders, "purchase_orders/mirror", order(PRODUCER));

    await assertFails(deleteDoc(doc(asProducer(orders), "purchase_orders/mirror")));
    await assertFails(deleteDoc(doc(asAgent(orders), "purchase_orders/mirror")));
  });

  it("lets any signed-in caller read one", async () => {
    // The agent reads orders to know an item is finished; the UI reads them for
    // the spend total. Reading is not the dangerous direction.
    await seed(orders, "purchase_orders/mirror", order(PRODUCER));

    await assertSucceeds(getDoc(doc(asAgent(orders), "purchase_orders/mirror")));
    await assertFails(getDoc(doc(asStranger(orders), "purchase_orders/mirror")));
  });

  it("denies everything else in the orders database", async () => {
    // The catch-all. If a future collection lands here without a rule, it is
    // closed rather than open.
    await assertFails(
      setDoc(doc(asProducer(orders), "budgets/projA"), { total: 1 }),
    );
  });
});

// --------------------------------------------------------------------------
// The default database — the agent's own workspace
// --------------------------------------------------------------------------

describe("negotiations", () => {
  const NEG = "projects/projA/negotiations/neg1";
  const record = (state: string) => ({
    item_id: "mirror",
    supplier_id: "sup1",
    state,
  });

  it("lets the agent run a negotiation", async () => {
    await assertSucceeds(
      setDoc(doc(asAgent(main), NEG), record("AWAITING_REPLY")),
    );
  });

  it("refuses to let the agent open one already marked ORDERED", async () => {
    await assertFails(setDoc(doc(asAgent(main), NEG), record("ORDERED")));
  });

  it("refuses to let the agent move one into ORDERED", async () => {
    // The stop condition, expressed in the database. Even with a compromised
    // prompt the agent cannot mark something bought.
    await seed(main, NEG, record("READY_FOR_HUMAN"));

    await assertFails(
      updateDoc(doc(asAgent(main), NEG), { state: "ORDERED" }),
    );
  });

  it("lets a producer move one into ORDERED", async () => {
    await seed(main, NEG, record("READY_FOR_HUMAN"));

    await assertSucceeds(
      updateDoc(doc(asProducer(main), NEG), { state: "ORDERED" }),
    );
  });

  it("refuses to let anyone delete a negotiation", async () => {
    await seed(main, NEG, record("READY_FOR_HUMAN"));

    await assertFails(deleteDoc(doc(asProducer(main), NEG)));
  });

  it("shuts an unauthenticated caller out entirely", async () => {
    await assertFails(setDoc(doc(asStranger(main), NEG), record("SENT")));
    await assertFails(getDoc(doc(asStranger(main), NEG)));
  });
});

describe("messages", () => {
  const MSG = "projects/projA/negotiations/neg1/messages/m1";
  const message = (body: string) => ({
    direction: "inbound",
    body,
    sim_sent_at: new Date("2026-03-01T09:00:00Z"),
  });

  it("appends", async () => {
    await assertSucceeds(
      setDoc(doc(asAgent(main), MSG), message("RM880 per unit.")),
    );
  });

  it("refuses to let the agent rewrite what a supplier said", async () => {
    // The timeline is the only evidence that days of negotiation happened. An
    // agent that could edit it would make that evidence worthless.
    await seed(main, MSG, message("RM880 per unit."));

    await assertFails(
      updateDoc(doc(asAgent(main), MSG), { body: "RM400 per unit." }),
    );
  });

  it("refuses to let even a producer rewrite one", async () => {
    await seed(main, MSG, message("RM880 per unit."));

    await assertFails(
      updateDoc(doc(asProducer(main), MSG), { body: "RM400 per unit." }),
    );
    await assertFails(deleteDoc(doc(asProducer(main), MSG)));
  });
});

describe("the split between the two databases", () => {
  it("has no purchase_orders collection in the default database", async () => {
    // If someone ever moves orders back alongside everything else, this fails.
    // The catch-all denies it, so a producer writing there is refused — which
    // is the signal that the collection genuinely does not live here.
    await assertFails(
      setDoc(
        doc(asProducer(main), "purchase_orders/mirror"),
        order(PRODUCER),
      ),
    );
  });
});

// --------------------------------------------------------------------------
// What the instrument panel may query
// --------------------------------------------------------------------------

describe("what a browser may query", () => {
  /**
   * These decide the shape of the panel's subscription rather than guarding
   * anything. Firestore evaluates a *list* against the rule for the documents
   * it would return, so "can read one document" does not imply "can list the
   * collection" — and a collection-group query is evaluated against a rule
   * matching `/{path=**}/negotiations/{id}`, which is not the nested rule we
   * wrote. Guessing either way costs an afternoon; asserting costs four lines.
   */

  const record = (state: string) => ({
    item_id: "mirror",
    supplier_id: "sup1",
    state,
  });

  beforeEach(async () => {
    await seed(main, "projects/projA", { title: "Kopitiam" });
    await seed(main, "projects/projA/negotiations/neg1", record("SENT"));
    await seed(main, "projects/projB/negotiations/neg2", record("SENT"));
    await seed(main, "projects/projA/negotiations/neg1/messages/m1", {
      direction: "inbound",
      body: "RM880 per unit.",
    });
  });

  it("lists the projects, so the panel can offer a choice", async () => {
    await assertSucceeds(getDocs(collection(asProducer(main), "projects")));
  });

  it("lists one project's negotiations", async () => {
    // The panel's main subscription. Scoped to a project, which is what the
    // nested rule authorises.
    await assertSucceeds(
      getDocs(collection(asProducer(main), "projects/projA/negotiations")),
    );
  });

  it("lists a negotiation's messages", async () => {
    // Phase 6's timeline, and the count shown per row today.
    await assertSucceeds(
      getDocs(
        collection(
          asProducer(main),
          "projects/projA/negotiations/neg1/messages",
        ),
      ),
    );
  });

  it("refuses a collection-group query across every project", async () => {
    // Settled by running it, not by reading the docs. The refusal comes from
    // the catch-all at firestore.rules:86, because a collection-group query is
    // matched against `/{path=**}/negotiations/{id}` and our rule is nested
    // under `/projects/{projectId}/` — which does not authorise one.
    //
    // So the panel subscribes per project. That is the cheaper landing anyway:
    // firestore.indexes.json carries a COLLECTION_GROUP index on
    // `next_action_due_at` and nothing else, so a group query ordered by any
    // other field would need a new index deployed as well. Widening the rules
    // to allow one would also hand a signed-in browser every project at once,
    // which is not something the panel needs.
    await assertFails(
      getDocs(collectionGroup(asProducer(main), "negotiations")),
    );
  });

  it("still shuts an unauthenticated caller out of every one of those", async () => {
    await assertFails(getDocs(collection(asStranger(main), "projects")));
    await assertFails(
      getDocs(collection(asStranger(main), "projects/projA/negotiations")),
    );
  });
});

// --------------------------------------------------------------------------
// mailboxes — whose Gmail the agent negotiates from
// --------------------------------------------------------------------------

describe("mailboxes", () => {
  const mailbox = (email: string) => ({
    email,
    display_name: "A Producer",
    status: "CONNECTED",
    connected_at: new Date("2026-03-01T09:00:00Z"),
    updated_at: new Date("2026-03-01T09:00:00Z"),
  });

  it("lets a producer read their own", async () => {
    await seed(main, `mailboxes/${PRODUCER}`, mailbox("me@example.test"));

    await assertSucceeds(
      getDoc(doc(asProducer(main), `mailboxes/${PRODUCER}`)),
    );
  });

  it("refuses to show one producer another's mailbox", async () => {
    // Which address a rival production negotiates from is not a browser's
    // business, and the document is keyed by uid precisely so the rule can
    // say that in one line.
    await seed(main, "mailboxes/someone-else", mailbox("them@example.test"));

    await assertFails(
      getDoc(doc(asProducer(main), "mailboxes/someone-else")),
    );
  });

  it("refuses a browser writing its own mailbox, claim or no claim", async () => {
    // The document records a consent that happened between a person and
    // Google. A browser that could edit it could mark a dead mailbox healthy
    // and strand every negotiation behind it — silently, which is the only
    // way that failure ever shows up.
    await assertFails(
      setDoc(doc(asProducer(main), `mailboxes/${PRODUCER}`), mailbox("me@x.test")),
    );
  });

  it("refuses an unauthenticated reader outright", async () => {
    await seed(main, `mailboxes/${PRODUCER}`, mailbox("me@example.test"));

    await assertFails(getDoc(doc(asStranger(main), `mailboxes/${PRODUCER}`)));
  });

  it("keeps the OAuth state binding away from browsers entirely", async () => {
    // Reading one is enough to finish somebody else's consent and attach
    // their mailbox to your own account.
    await seed(main, "oauth_states/abc123", { uid: PRODUCER });

    await assertFails(getDoc(doc(asProducer(main), "oauth_states/abc123")));
    await assertFails(
      setDoc(doc(asProducer(main), "oauth_states/mine"), { uid: PRODUCER }),
    );
  });
});
