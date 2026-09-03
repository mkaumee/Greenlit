/**
 * Where the agent stops.
 *
 * `READY_FOR_HUMAN` is the stop condition the whole system is built around: it
 * reads scripts, prices props and negotiates for days on its own, and the one
 * thing it cannot do is spend money. So this card is not a status update. It
 * is the only place in the product where a purchase can begin, and it renders
 * as a `tool-call` part carrying an `approval` because that is exactly what
 * assistant-ui models: the assistant wants to do something a human must
 * authorise.
 *
 * It is deliberately not the only place this decision appears. `Chat.tsx` also
 * puts it in the inspector, which does not scroll — a purchase a producer has
 * to scroll back to find is a purchase they will miss.
 *
 * Approving posts to `cinema-approvals`, a different service under a different
 * service account: the only identity in the system with an IAM binding on the
 * `orders` database. Nothing on this page could write a purchase order even if
 * it tried.
 */

import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { useState } from "react";

import { approve, type Outcome } from "@/approvals";
import { useProjectId } from "@/components/chat/context";
import { Button } from "@/components/ui/button";

export interface DecisionArgs {
  itemId?: string;
  item?: string;
  supplier?: string;
  price?: string;
  rounds?: number;
  reason?: string;
  reasoning?: string;
  rivals?: number;
}

export function DecisionPart({
  args,
  approval,
}: ToolCallMessagePartProps<DecisionArgs, unknown>) {
  // The negotiation id travels on the approval rather than in the args: that
  // is what `convert.ts` puts there and what the library uses to identify the
  // pending decision, so reading it from anywhere else would be a second
  // source for the same value.
  const projectId = useProjectId();
  return (
    <Decision args={args} negotiationId={approval?.id ?? ""} projectId={projectId} />
  );
}

export function Decision({
  args,
  negotiationId,
  projectId,
  compact = false,
}: {
  args: DecisionArgs;
  negotiationId: string;
  projectId: string;
  /** The rail's version: same decision, less of it, still one click. */
  compact?: boolean;
}) {
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);

  const run = () => {
    setBusy(true);
    void approve(projectId, args.itemId ?? "", negotiationId)
      .then(setOutcome)
      .finally(() => setBusy(false));
  };

  return (
    <div className="my-2 rounded-lg border-2 border-foreground/40 px-4 py-3 text-sm">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Needs you
      </p>
      <p className="mt-1 font-medium capitalize">
        {args.item ?? "an item"} — {args.supplier ?? "a seller"} at{" "}
        {args.price ?? "no price yet"}
      </p>
      <p className="mt-1 text-muted-foreground">
        {args.rounds ?? 0} round{args.rounds === 1 ? "" : "s"} of negotiation.
        {args.reason !== undefined && args.reason !== "" ? ` ${args.reason}.` : ""}
      </p>
      {args.rivals !== undefined && args.rivals > 0 && (
        // Said out loud because approving is keyed by the item: it settles
        // every quote for this prop, not just the one on this card.
        <p className="mt-1 text-xs text-muted-foreground">
          {args.rivals} other quote{args.rivals === 1 ? "" : "s"} for this prop,
          all dearer. Approving settles them too.
        </p>
      )}
      {!compact && args.reasoning !== undefined && args.reasoning !== "" && (
        // The agent's own last explanation, carried through from the
        // negotiation record. Why it stopped here, in its words.
        <p className="mt-2 border-l-2 pl-3 text-muted-foreground italic">
          {args.reasoning}
        </p>
      )}

      {outcome === null ? (
        <div className="mt-3 flex items-center gap-3">
          <Button size="sm" disabled={busy} onClick={run}>
            {busy ? "Approving…" : "Approve"}
          </Button>
          <span className="text-xs text-muted-foreground">
            Nothing is bought until you press this.
          </span>
        </div>
      ) : (
        <Result outcome={outcome} />
      )}
    </div>
  );
}

function Result({ outcome }: { outcome: Outcome }) {
  switch (outcome.kind) {
    case "approved":
      return (
        <p className="mt-3 font-medium">
          {outcome.alreadyExisted
            ? "Already ordered — the existing purchase order stands."
            : `Ordered at ${outcome.price.currency} ${outcome.price.amount.toLocaleString()}.`}
        </p>
      );
    case "duplicate":
      // The most defensible thing this system does, so it is shown rather than
      // swallowed: a purchase order is created with create() keyed by the
      // item, so a second one is refused by the storage engine before any of
      // our code runs — including from a different supplier.
      return (
        <p className="mt-3 text-muted-foreground">
          Refused: this item is already ordered. {outcome.detail}
        </p>
      );
    case "forbidden":
      return (
        <p className="mt-3 text-destructive">
          You are not a producer on this deployment. {outcome.detail}
        </p>
      );
    default:
      return <p className="mt-3 text-destructive">{outcome.detail}</p>;
  }
}
