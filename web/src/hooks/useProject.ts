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
export function useProjects(): { ids: string[]; error: string } {
  const [state, setState] = useState<{ ids: string[]; error: string }>({
    ids: [],
    error: "",
  });

  useEffect(() => {
    const stop = onAuthStateChanged(auth, (user) => {
      if (user === null) {
        setState({ ids: [], error: "" });
        return;
      }
      return onSnapshot(
        query(collection(db, "projects"), where("owner_uid", "==", user.uid)),
        (snap) => setState({ ids: snap.docs.map((d) => d.id), error: "" }),
        (cause) => setState({ ids: [], error: cause.message }),
      );
    });
    return stop;
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
