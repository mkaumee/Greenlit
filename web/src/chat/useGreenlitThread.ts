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
import { ask, toUpload, uploadScript, type Upload } from "./api";
import { IDLE, isBusy, type Busy } from "./busy";
import { decisionsFor } from "./decisions";
import { researchOf, stillWorking } from "./research";
import { toThreadMessage } from "./convert";
import { directionOf, inOrder, type Row } from "./rows";

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
  /** Opening emails written and not yet released. Also waiting on a person. */
  openings: Row[];
  /** Hand a screenplay to the agent. Resolves once the props are on screen. */
  readScript: (file: File) => Promise<void>;
  /**
   * What is in flight, for the indicator in the transcript.
   *
   * Richer than the runtime's own `isRunning` on purpose: reading a screenplay
   * and answering a question are both "running" and feel nothing alike, so the
   * screen has to be able to say which. `isRunning` is still derived from this
   * rather than tracked beside it, so the two cannot disagree.
   */
  busy: Busy;
}

export function useGreenlitThread(sources: ThreadSources): GreenlitThread {
  const [conversation, setConversation] = useState<Row[]>([]);
  const [busy, setBusy] = useState<Busy>(IDLE);

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
          direction: directionOf(message.direction),
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

  // One row per prop, not per negotiation. The agent approaches several
  // suppliers for the same item, so `READY_FOR_HUMAN` can be true of three at
  // once — and `purchase_orders` is keyed by the item, meaning approving any
  // one of them makes the others unapprovable. Offering all three as separate
  // decisions would put an Approve button under the expensive quote sitting
  // beside the cheap one. `decisionsFor` picks the cheapest and demotes the
  // rest, which is the rule the Inbox already used.
  const waiting = useMemo<Row[]>(
    () =>
      decisionsFor(sources.items, sources.negotiations).map(
        ({ item, chosen, rivals }) => ({
          kind: "decision" as const,
          id: `decision:${chosen.id}`,
          negotiationId: chosen.id,
          itemId: item.id,
          itemName: item.name ?? item.id,
          supplier: named(chosen.supplier_id),
          price: money(chosen.latest_quote),
          roundsUsed: chosen.rounds_used ?? 0,
          reason: chosen.escalation_reason ?? "",
          reasoning: chosen.latest_reasoning ?? "",
          rivals: rivals.length,
          at: chosen.last_inbound_at?.toDate() ?? new Date(),
        }),
      ),
    [sources.items, sources.negotiations, named],
  );

  /**
   * Opening emails written and not yet released.
   *
   * Derived from Firestore rather than pushed in like the props are, because
   * they appear on their own a tick or two after confirmation — nobody is
   * watching when the research finishes, and the screen filling itself is the
   * thing this product claims.
   *
   * One row for the batch, dated by the oldest, so it sorts into the
   * transcript where the drafting happened rather than jumping to the bottom
   * every time another one lands.
   */
  const openings = useMemo<Row[]>(() => {
    const pending = sources.negotiations
      .filter(
        (n) =>
          n.state === "DRAFTED" &&
          (n.draft_body ?? "") !== "" &&
          n.opening_released_at == null,
      )
      .map((n) => ({
        negotiationId: n.id,
        supplier: named(n.supplier_id),
        itemName: itemNamed(n.item_id),
        subject: n.draft_subject ?? "",
        body: n.draft_body ?? "",
      }));
    if (pending.length === 0) return [];

    const at = sources.negotiations
      .filter((n) => pending.some((p) => p.negotiationId === n.id))
      .map((n) => n.updated_at?.toDate())
      .filter((d): d is Date => d !== undefined)
      .sort((a, b) => a.getTime() - b.getTime())[0];

    return [
      {
        kind: "openings" as const,
        id: "openings",
        openings: pending,
        at: at ?? new Date(),
      },
    ];
  }, [sources.negotiations, named, itemNamed]);

  /**
   * The agent working, between confirmation and the first drafts.
   *
   * Dated by the oldest item still in flight so it sits where the work started
   * rather than jumping to the bottom on every snapshot — this row updates
   * several times a minute while a tick runs, and a card that keeps relocating
   * is harder to read than one that stays put and fills in.
   */
  const research = useMemo<Row[]>(() => {
    const items = researchOf(sources.items, sources.suppliers, sources.negotiations);
    if (items.length === 0) return [];
    return [
      {
        kind: "research" as const,
        id: "research",
        items,
        running: stillWorking(items),
        at: sources.items
          .map((i) => i.next_action_due_at?.toDate())
          .filter((d): d is Date => d !== undefined)
          .sort((a, b) => a.getTime() - b.getTime())[0] ?? new Date(),
      },
    ];
  }, [sources.items, sources.suppliers, sources.negotiations]);

  const rows = useMemo(
    () => inOrder([...conversation, ...activity, ...waiting, ...openings, ...research]),
    [conversation, activity, waiting, openings, research],
  );


  /**
   * Nothing can be uploaded to or asked about a production that does not
   * exist. The controls are disabled for this, so reaching here means a code
   * path found its way round them — say so in the thread rather than posting
   * to `/projects//script`, which matches no route and answers with a 404 that
   * explains nothing.
   */
  const noProduction = useCallback((): boolean => {
    if (sources.projectId !== "") return false;
    const at = new Date();
    setConversation((prior) => [
      ...prior,
      {
        kind: "briefing",
        id: `n:${at.getTime()}`,
        text:
          "There is no production yet. Start one under New production, and " +
          "then the screenplay has somewhere to go.",
        refs: [],
        at,
      },
    ]);
    return true;
  }, [sources.projectId]);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const question = textOf(message);
      if (question === "") return;
      if (noProduction()) return;

      const at = new Date();
      setConversation((prior) => [
        ...prior,
        { kind: "producer", id: `p:${at.getTime()}`, text: question, at },
      ]);
      setBusy({ kind: "asking" });
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
            fromStoredFacts:
              answer.kind === "answered" && answer.source === "stored-facts",
            reason: answer.kind === "answered" ? answer.fallbackReason : "",
            at: repliedAt,
          },
        ]);
      } finally {
        // In a finally because a thread stuck at "running" disables the
        // composer, and a producer who cannot type has no way to find out why.
        setBusy(IDLE);
      }
    },
    [sources.projectId, noProduction],
  );

  /**
   * Read a screenplay and put the props in the transcript.
   *
   * A conversation turn rather than a modal: reading a script is something the
   * agent did, and it belongs in the record beside the emails it sent. The
   * producer's line goes in first so the transcript reads as a thing they
   * asked for rather than a panel that appeared.
   */
  const readScript = useCallback(
    async (file: File) => {
      if (noProduction()) return;
      const at = new Date();
      setConversation((prior) => [
        ...prior,
        {
          kind: "producer",
          id: `p:${at.getTime()}`,
          text: `Read ${file.name}.`,
          at,
        },
      ]);
      setBusy({ kind: "reading", filename: file.name });
      try {
        const upload: Upload = await toUpload(file);
        const result = await uploadScript(sources.projectId, upload);
        const readAt = new Date();
        setConversation((prior) => [
          ...prior,
          result.kind === "read"
            ? {
                kind: "props",
                id: `s:${readAt.getTime()}`,
                filename: file.name,
                props: result.props,
                at: readAt,
              }
            : {
                // An unreadable file is not an error to swallow: the message
                // says a scan is a scan and what to do instead, and it is
                // written for the person reading it.
                kind: "briefing",
                id: `s:${readAt.getTime()}`,
                text: result.detail,
                refs: [],
                at: readAt,
              },
        ]);
      } finally {
        setBusy(IDLE);
      }
    },
    [sources.projectId, noProduction],
  );

  const runtime = useExternalStoreRuntime<Row>({
    messages: rows,
    isRunning: isBusy(busy),
    onNew,
    convertMessage: toThreadMessage,
  });

  return { runtime, waiting, openings, readScript, busy };
}
