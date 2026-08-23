/**
 * The panel's subscription.
 *
 * `onSnapshot` rather than a fetch on an interval — "it updates on its own" is
 * the thing this screen exists to demonstrate, and a refresh button would let
 * a broken loop look alive.
 *
 * Scoped to one project because a collection-group query is refused: Firestore
 * matches one against `/{path=**}/negotiations/{id}` and our rule is nested
 * under `/projects/{projectId}/`, so it falls through to the catch-all deny.
 * That is asserted in `tests/rules.test.ts` rather than assumed.
 */

import {
  collection,
  getCountFromServer,
  onSnapshot,
  query,
  type Timestamp,
} from "firebase/firestore";
import { useEffect, useState } from "react";

import { db } from "./firebase";

export interface Money {
  amount: number;
  currency: string;
}

export interface Quote {
  unit_price?: Money;
  total?: Money;
  qty?: number;
}

/**
 * Mirrors `orchestrator.records.NegotiationRecord` — the fields this screen
 * reads, and only those. Everything is optional because the record model
 * allows extra fields and omits absent ones: `next_action_due_at` is deleted
 * rather than nulled once a negotiation is terminal, which is what drops it
 * out of the tick's index.
 */
export interface Negotiation {
  id: string;
  item_id?: string;
  supplier_id?: string;
  state?: string;
  rounds_used?: number;
  max_rounds?: number;
  latest_quote?: Quote;
  next_action_due_at?: Timestamp;
  last_inbound_at?: Timestamp;
  escalation_reason?: string;
  messages?: number;
}

export interface Feed {
  rows: Negotiation[];
  error: string;
  loading: boolean;
}

export function useNegotiations(projectId: string): Feed {
  const [feed, setFeed] = useState<Feed>({
    rows: [],
    error: "",
    loading: true,
  });

  useEffect(() => {
    if (projectId === "") {
      setFeed({ rows: [], error: "", loading: false });
      return;
    }

    setFeed({ rows: [], error: "", loading: true });

    // Deliberately unordered. Ordering server-side would need an index
    // deployed for whichever field we picked, and a dozen rows sort fine in
    // the browser.
    const path = collection(db, "projects", projectId, "negotiations");

    let live = true;
    const stop = onSnapshot(
      query(path),
      (snap) => {
        const rows = snap.docs.map((d) => ({
          id: d.id,
          ...(d.data() as Omit<Negotiation, "id">),
        }));
        rows.sort((a, b) => (a.item_id ?? "").localeCompare(b.item_id ?? ""));

        // Carry the counts already fetched across the update. Without this,
        // every snapshot blanks the whole column to a placeholder for as long
        // as the aggregations take — which on a screen whose point is that it
        // changes on its own reads as a glitch.
        setFeed((prev) => {
          const known = new Map(prev.rows.map((r) => [r.id, r.messages]));
          return {
            rows: rows.map((r) => ({ ...r, messages: known.get(r.id) })),
            error: "",
            loading: false,
          };
        });

        // Message counts are the one column not on the negotiation document.
        // One aggregation per row per snapshot, rather than N live listeners —
        // the wrong trade for a debug panel, and it would multiply the
        // connection count by the number of negotiations.
        //
        // The consequence: a message appearing on its own does not refresh the
        // count, because a subcollection write does not fire the parent's
        // snapshot. That is fine here — the tick writes the message and the
        // negotiation together, so a reply moves both.
        void Promise.all(
          rows.map(async (row) => {
            const messages = collection(path, row.id, "messages");
            const count = await getCountFromServer(messages);
            return { id: row.id, messages: count.data().count };
          }),
        ).then((counts) => {
          if (!live) return;
          const byId = new Map(counts.map((c) => [c.id, c.messages]));
          setFeed((prev) => ({
            ...prev,
            rows: prev.rows.map((r) => ({
              ...r,
              messages: byId.get(r.id) ?? r.messages,
            })),
          }));
        });
      },
      (cause) => {
        setFeed({ rows: [], error: cause.message, loading: false });
      },
    );

    return () => {
      live = false;
      stop();
    };
  }, [projectId]);

  return feed;
}

/** Every project, live. Small enough that the panel just lists them all. */
export function useProjects(): { ids: string[]; error: string } {
  const [state, setState] = useState<{ ids: string[]; error: string }>({
    ids: [],
    error: "",
  });

  useEffect(() => {
    return onSnapshot(
      collection(db, "projects"),
      (snap) => {
        setState({ ids: snap.docs.map((d) => d.id), error: "" });
      },
      (cause) => {
        setState({ ids: [], error: cause.message });
      },
    );
  }, []);

  return state;
}
