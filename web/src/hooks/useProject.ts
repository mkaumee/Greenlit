/**
 * The live reads the product screens are built on.
 *
 * Scoped per project throughout, because `tests/rules.test.ts` settled that a
 * collection-group query over negotiations is refused — Firestore matches one
 * against `/{path=**}/negotiations/{id}` and our rule is nested under
 * `/projects/{projectId}/`. That is asserted, not assumed.
 *
 * Everything is `onSnapshot`. The agent works for days while nobody watches,
 * so a screen that only updates on reload would be lying about a system whose
 * whole point is that it moves on its own.
 */

import {
  collection,
  doc,
  onSnapshot,
  orderBy,
  query,
  where,
  type Timestamp,
} from "firebase/firestore";
import { onAuthStateChanged } from "firebase/auth";
import { useEffect, useState } from "react";

import type { MailboxDoc } from "@/chat/mailbox";
import { auth, db } from "@/firebase";

export interface Money {
  amount: number;
  currency: string;
}

export interface Quote {
  unit_price?: Money;
  total?: Money;
  qty?: number;
  lead_time_days?: number | null;
  terms?: string;
}

export interface ReferenceBand {
  low?: Money;
  high?: Money;
  note?: string;
  /** The pages the numbers came from.
   *
   * Stored on every item since research first ran, and dropped here until now:
   * this interface declared only the range, so the panel rendered a band with
   * no way to tell a researched number from one the model remembered. That is
   * the opposite of the claim the whole system rests on. */
  source_urls?: string[];
}

/** A line of the screenplay this prop was found in — the evidence a producer
 * checks the agent against, so it belongs on screen and not behind a link. */
export interface SceneMention {
  scene?: string;
  line?: string;
  quote?: string;
}

export interface Item {
  id: string;
  name?: string;
  category?: string;
  qty?: number;
  consumable?: boolean;
  scenes?: string[];
  mentions?: SceneMention[];
  reference_band?: ReferenceBand;
  status?: string;
  floor_price?: Money;
  chosen_quote?: Quote;
  next_action_due_at?: Timestamp;
}

export interface Negotiation {
  id: string;
  item_id?: string;
  supplier_id?: string;
  state?: string;
  rounds_used?: number;
  max_rounds?: number;
  first_quote?: Quote;
  latest_quote?: Quote;
  floor_price?: Money;
  next_action_due_at?: Timestamp;
  last_inbound_at?: Timestamp;
  last_outbound_at?: Timestamp;
  escalation_reason?: string;
  latest_reasoning?: string;
}

export interface Supplier {
  id: string;
  name?: string;
  email?: string;
  source_url?: string;
  verified?: boolean;
}

export interface Message {
  id: string;
  direction?: string;
  subject?: string;
  body?: string;
  sim_sent_at?: Timestamp;
}

/** One live collection under a project. The single shape every screen reads. */
function useCollection<T>(projectId: string, path: string): {
  rows: T[];
  error: string;
  loading: boolean;
} {
  const [state, setState] = useState<{
    rows: T[];
    error: string;
    loading: boolean;
  }>({ rows: [], error: "", loading: true });

  useEffect(() => {
    if (projectId === "") {
      setState({ rows: [], error: "", loading: false });
      return;
    }
    setState({ rows: [], error: "", loading: true });
    return onSnapshot(
      collection(db, "projects", projectId, ...path.split("/")),
      (snap) => {
        setState({
          rows: snap.docs.map((d) => ({ id: d.id, ...d.data() }) as T),
          error: "",
          loading: false,
        });
      },
      (cause) => setState({ rows: [], error: cause.message, loading: false }),
    );
  }, [projectId, path]);

  return state;
}

export const useItems = (projectId: string) =>
  useCollection<Item>(projectId, "items");

export const useNegotiations = (projectId: string) =>
  useCollection<Negotiation>(projectId, "negotiations");

export const useSuppliers = (projectId: string) =>
  useCollection<Supplier>(projectId, "suppliers");

/**
 * The productions this account owns. Nobody else's.
 *
 * This listed every project until somebody signed in with a second Google
 * account and was shown a stranger's props, suppliers, quotes and negotiation
 * transcripts. The query asked for all of them and `firestore.rules` said
 * `isSignedIn()`, which is true of every Google account there is.
 *
 * The filter and the rule are both needed and do different jobs: the filter is
 * what this screen asks for, the rule is what makes it true for anything else
 * that opens the collection. Neither alone is the boundary.
 *
 * Returns nothing while signed out rather than erroring — that is a state the
 * shell renders, not a failure.
 */
export function useProjects(): {
  ids: string[];
  error: string;
  loading: boolean;
} {
  // Starts loading, unlike `useCollection` which is handed a project id it can
  // check. Here there is nothing to check: until the auth observer fires there
  // is no user, and until the first snapshot lands "no productions" and "not
  // asked yet" are the same empty array. Telling them apart is the whole
  // reason this flag exists — a producer who owns three productions should not
  // be shown "none yet" on the way in.
  const [state, setState] = useState<{
    ids: string[];
    error: string;
    loading: boolean;
  }>({ ids: [], error: "", loading: true });

  useEffect(() => {
    // Same leak as useMailbox had: what an auth observer returns is discarded,
    // so the previous user's query kept running after a re-sign-in and could
    // overwrite the new one's results with its own failure.
    let stopSnapshot: (() => void) | undefined;
    const close = () => {
      stopSnapshot?.();
      stopSnapshot = undefined;
    };

    const stopAuth = onAuthStateChanged(auth, (user) => {
      close();
      if (user === null) {
        // Signed out is an answer, not a wait.
        setState({ ids: [], error: "", loading: false });
        return;
      }
      setState({ ids: [], error: "", loading: true });
      stopSnapshot = onSnapshot(
        query(collection(db, "projects"), where("owner_uid", "==", user.uid)),
        (snap) =>
          setState({
            ids: snap.docs.map((d) => d.id),
            error: "",
            loading: false,
          }),
        (cause) => setState({ ids: [], error: cause.message, loading: false }),
      );
    });

    return () => {
      close();
      stopAuth();
    };
  }, []);

  return state;
}

/**
 * This account's mailbox record, live.
 *
 * `GET /mailbox` on `cinema-api` answers the same question and is not used
 * here: the card has to change the moment the OAuth callback writes, and that
 * write happens in a popup this page never hears from. A snapshot is what
 * closes that loop — the popup closes itself, Firestore pushes, the card
 * flips.
 *
 * ## Four states, not three, and none of them overloaded
 *
 * This returned `MailboxDoc | null | undefined`, where `undefined` meant both
 * "not read yet" and "the read failed" — and `cardFor` rendered both as the
 * Connect button. So a producer whose mailbox was connected, whose token was in
 * Secret Manager and whose record was in Firestore, was shown a button asking
 * them to connect it. The comment here used to assert that a failure was "a
 * signed-out race rather than a mailbox that is missing", which was a guess,
 * and wrong: the deployed security rules were behind the code and the read was
 * being denied outright. Nothing on the screen could say so.
 *
 * The failure this system is built to avoid is a supplier's silence being
 * indistinguishable from a broken transport. This was the same shape of
 * mistake, in the browser.
 */
export type MailboxState =
  | { status: "loading" }
  | { status: "none" }
  | { status: "have"; record: MailboxDoc }
  | { status: "denied"; detail: string };

export function useMailbox(): MailboxState {
  const [state, setState] = useState<MailboxState>({ status: "loading" });

  useEffect(() => {
    // Firebase ignores what an auth observer returns, so `return onSnapshot(…)`
    // inside one unsubscribes nothing: the listener opened for the previous
    // user outlives them. Signing out and back in then leaves two listeners on
    // the same document, and the stale one — now failing, because it is reading
    // as nobody — overwrites the live one's value. Hence a variable the
    // effect's own cleanup can close over.
    let stopSnapshot: (() => void) | undefined;
    const close = () => {
      stopSnapshot?.();
      stopSnapshot = undefined;
    };

    const stopAuth = onAuthStateChanged(auth, (user) => {
      close();
      if (user === null) {
        setState({ status: "none" });
        return;
      }
      setState({ status: "loading" });
      stopSnapshot = onSnapshot(
        doc(db, "mailboxes", user.uid),
        (snap) =>
          setState(
            snap.exists()
              ? { status: "have", record: snap.data() as MailboxDoc }
              : { status: "none" },
          ),
        (cause) => setState({ status: "denied", detail: cause.message }),
      );
    });

    return () => {
      close();
      stopAuth();
    };
  }, []);

  return state;
}

/** The transcript of one negotiation, oldest first — the proof days passed. */
export function useMessages(projectId: string, negotiationId: string) {
  const [rows, setRows] = useState<Message[]>([]);

  useEffect(() => {
    if (projectId === "" || negotiationId === "") return;
    return onSnapshot(
      query(
        collection(
          db,
          "projects",
          projectId,
          "negotiations",
          negotiationId,
          "messages",
        ),
        orderBy("sim_sent_at"),
      ),
      (snap) =>
        setRows(snap.docs.map((d) => ({ id: d.id, ...d.data() }) as Message)),
      () => setRows([]),
    );
  }, [projectId, negotiationId]);

  return rows;
}

/**
 * Every negotiation's correspondence at once, keyed by negotiation.
 *
 * The chat transcript is mostly not a conversation — it is the emails the
 * agent sent and the replies it got, from every negotiation on the production,
 * interleaved in the order they happened. That needs all of them.
 *
 * A collection-group query over `messages` would be one subscription instead
 * of N, and is not available: `tests/rules.test.ts` asserts Firestore refuses
 * one, because it matches a group query against `/{path=**}/messages/{id}`
 * while our rule is nested under `/projects/{projectId}/`. So this holds one
 * `onSnapshot` per negotiation, which is what the panel was already doing —
 * just from one place rather than from a component per row.
 *
 * The ids are joined into the dependency rather than passed as an array: a
 * parent that re-renders hands down a new array with the same contents every
 * time, and comparing by reference would tear down and rebuild every
 * subscription on each render.
 */
export function useAllMessages(
  projectId: string,
  negotiationIds: string[],
): { rows: Record<string, Message[]>; loading: boolean } {
  const [byNegotiation, setByNegotiation] = useState<Record<string, Message[]>>({});
  const key = negotiationIds.join(",");

  useEffect(() => {
    const ids = key === "" ? [] : key.split(",");
    if (projectId === "" || ids.length === 0) {
      setByNegotiation({});
      return;
    }

    // Replaces rather than merges, so a negotiation that has gone away does
    // not leave its messages behind in the transcript for the rest of the
    // session. Each subscription fills its own key back in immediately.
    setByNegotiation({});

    const stops = ids.map((id) =>
      onSnapshot(
        query(
          collection(db, "projects", projectId, "negotiations", id, "messages"),
          orderBy("sim_sent_at"),
        ),
        (snap) =>
          setByNegotiation((prior) => ({
            ...prior,
            [id]: snap.docs.map((d) => ({ id: d.id, ...d.data() }) as Message),
          })),
        // One negotiation failing to read must not empty the others. It is
        // almost always a rules refusal on a project that is not ours, and the
        // rest of the transcript is still true.
        () => setByNegotiation((prior) => ({ ...prior, [id]: [] })),
      ),
    );
    return () => stops.forEach((stop) => stop());
  }, [projectId, key]);

  // Every subscription writes its own key on its first snapshot, including the
  // failure path, so a short count means at least one negotiation has not
  // reported yet. Worth telling the transcript about: this is the window where
  // it has negotiations but no correspondence, which looks exactly like a
  // production nobody has uploaded a script to.
  const expected = key === "" ? 0 : key.split(",").length;
  const loading = expected > 0 && Object.keys(byNegotiation).length < expected;

  return { rows: byNegotiation, loading };
}

export function useItem(projectId: string, itemId: string) {
  const [item, setItem] = useState<Item | null>(null);

  useEffect(() => {
    if (projectId === "" || itemId === "") return;
    return onSnapshot(
      doc(db, "projects", projectId, "items", itemId),
      (snap) =>
        setItem(snap.exists() ? ({ id: snap.id, ...snap.data() } as Item) : null),
      () => setItem(null),
    );
  }, [projectId, itemId]);

  return item;
}
