/**
 * One thread, three sources.
 *
 * The producer's turns and the agent's answers live in React state — a chat
 * transcript is not evidence and has no business sitting in Firestore beside
 * the correspondence that is. The other two sources are the correspondence:
 * every email the agent sent and every reply it got, already stored, already
 * subscribed to by `useProject.ts`.
 *
 * That is what makes this worth building on an external store rather than a
 * chat runtime. Most of what appears here was not said to anyone — it is a
 * system working while nobody watched, and the screen updating on its own is
 * the claim being demonstrated.
 */

import { useExternalStoreRuntime } from "@assistant-ui/react";
import type { AppendMessage, AssistantRuntime } from "@assistant-ui/react";
import { useCallback, useMemo, useState } from "react";

import type { Item, Message, Negotiation, Supplier } from "@/hooks/useProject";
import { ask } from "./api";
import { toThreadMessage } from "./convert";
import { inOrder, type Row } from "./rows";

const WAITING = "READY_FOR_HUMAN";

const money = (q: Negotiation["latest_quote"]): string => {
  const price = q?.unit_price ?? q?.total;
  return price ? `${price.currency} ${price.amount.toLocaleString()}` : "no price yet";
};

const textOf = (message: AppendMessage): string =>
  message.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("")
    .trim();

export interface ThreadSources {
  projectId: string;
  items: Item[];
  negotiations: Negotiation[];
  suppliers: Supplier[];
  /** Correspondence, keyed by negotiation. */
  messages: Record<string, Message[]>;
}

export interface GreenlitThread {
  runtime: AssistantRuntime;
  /** Decisions waiting on a person. The rail reads this; it never scrolls. */
  waiting: Row[];
}

export function useGreenlitThread(sources: ThreadSources): GreenlitThread {
  const [conversation, setConversation] = useState<Row[]>([]);
  const [running, setRunning] = useState(false);

  const named = useCallback(
    (id: string | undefined) =>
      sources.suppliers.find((s) => s.id === id)?.name ?? id ?? "unknown seller",
    [sources.suppliers],
  );

  const itemNamed = useCallback(
    (id: string | undefined) =>
      sources.items.find((i) => i.id === id)?.name ?? id ?? "an item",
    [sources.items],
  );

  const activity = useMemo<Row[]>(() => {
    const rows: Row[] = [];
    for (const negotiation of sources.negotiations) {
      for (const message of sources.messages[negotiation.id] ?? []) {
        const at = message.sim_sent_at?.toDate();
        // No timestamp means the document is mid-write, and a row placed at
        // the epoch would jump to the top of the transcript. Skipping is the
        // honest option: the next snapshot brings it back complete.
        if (!at) continue;
        rows.push({
          kind: "activity",
          id: `${negotiation.id}:${message.id}`,
          negotiationId: negotiation.id,
          direction: message.direction === "inbound" ? "inbound" : "outbound",
          supplier: named(negotiation.supplier_id),
          itemName: itemNamed(negotiation.item_id),
          subject: message.subject ?? "",
          body: message.body ?? "",
          at,
        });
      }
    }
    return rows;
  }, [sources.negotiations, sources.messages, named, itemNamed]);

  const waiting = useMemo<Row[]>(
    () =>
      sources.negotiations
        .filter((n) => n.state === WAITING)
        .map((n) => ({
          kind: "decision" as const,
          id: `decision:${n.id}`,
          negotiationId: n.id,
          itemId: n.item_id ?? "",
          itemName: itemNamed(n.item_id),
          supplier: named(n.supplier_id),
          price: money(n.latest_quote),
          roundsUsed: n.rounds_used ?? 0,
          reason: n.escalation_reason ?? "",
          reasoning: n.latest_reasoning ?? "",
          at: n.last_inbound_at?.toDate() ?? new Date(),
        })),
    [sources.negotiations, named, itemNamed],
  );

  const rows = useMemo(
    () => inOrder([...conversation, ...activity, ...waiting]),
    [conversation, activity, waiting],
  );

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const question = textOf(message);
      if (question === "") return;

      const at = new Date();
      setConversation((prior) => [
        ...prior,
        { kind: "producer", id: `p:${at.getTime()}`, text: question, at },
      ]);
      setRunning(true);
      try {
        const answer = await ask(sources.projectId, question);
        const repliedAt = new Date();
        setConversation((prior) => [
          ...prior,
          {
            kind: "briefing",
            id: `b:${repliedAt.getTime()}`,
            text: answer.kind === "answered" ? answer.text : answer.detail,
            refs: answer.kind === "answered" ? answer.refs : [],
            at: repliedAt,
          },
        ]);
      } finally {
        // In a finally because a thread stuck at "running" disables the
        // composer, and a producer who cannot type has no way to find out why.
        setRunning(false);
      }
    },
    [sources.projectId],
  );

  const runtime = useExternalStoreRuntime<Row>({
    messages: rows,
    isRunning: running,
    onNew,
    convertMessage: toThreadMessage,
  });

  return { runtime, waiting };
}
