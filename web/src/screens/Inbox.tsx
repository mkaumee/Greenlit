/**
 * What needs a person. The home screen, and the point of the product.
 *
 * The agent reads scripts, researches prices and negotiates for days on its
 * own. The only thing it cannot do is spend money, so the only screen that has
 * to exist is the one where a human decides. Everything else is background.
 *
 * Each card carries its own evidence inline rather than behind a link. That is
 * deliberate: the research on agent interfaces is consistent that people say
 * citations increase their confidence and then never click one, so evidence a
 * producer has to go looking for is decoration. The script line, what the
 * seller opened at, how many rounds it took and what the market looks like are
 * all on the card.
 */

import { useState } from "react";

import { approve, setFloor, type Outcome } from "@/approvals";
import { decisionsFor as pending, type Decision } from "@/chat/decisions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import type { Item, Negotiation } from "@/hooks/useProject";
import { money, saving, simTime } from "@/lib/format";



export function Inbox({
  projectId,
  items,
  negotiations,
  supplierName,
}: {
  projectId: string;
  items: Item[];
  negotiations: Negotiation[];
  supplierName: (id: string | undefined) => string;
}) {
  const decisions = pending(items, negotiations);
  const running = negotiations.filter(
    (n) => n.state === "SENT" || n.state === "AWAITING_REPLY",
  ).length;

  if (decisions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-10 text-center">
        <p className="text-lg font-medium">Nothing needs you.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          {running > 0
            ? `${String(running)} negotiation${running === 1 ? "" : "s"} still running. This fills in on its own.`
            : "No negotiations are waiting on a reply."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xl font-semibold">
          {decisions.length} decision{decisions.length === 1 ? "" : "s"} waiting
        </h2>
        <p className="text-sm text-muted-foreground">
          The agent stops here every time. Nothing is bought without you.
        </p>
      </div>
      {decisions.map((d) => (
        <DecisionCard
          key={d.item.id}
          projectId={projectId}
          decision={d}
          supplierName={supplierName}
        />
      ))}
    </div>
  );
}

function DecisionCard({
  projectId,
  decision,
  supplierName,
}: {
  projectId: string;
  decision: Decision;
  supplierName: (id: string | undefined) => string;
}) {
  const { item, chosen, rivals } = decision;
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const won = saving(chosen.first_quote, chosen.latest_quote);
  const line = item.mentions?.[0];

  const run = (action: () => Promise<Outcome>) => () => {
    setBusy(true);
    void action()
      .then(setOutcome)
      .finally(() => setBusy(false));
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-lg capitalize">{item.name ?? item.id}</CardTitle>
          {item.consumable === true && (
            <Badge variant="secondary">destroyed on camera · ×{item.qty ?? 1}</Badge>
          )}
          {chosen.escalation_reason !== undefined &&
            chosen.escalation_reason !== "" && (
              <Badge variant="outline">{chosen.escalation_reason}</Badge>
            )}
        </div>

        {/* The script line, inline. This is the answer to "why does the shoot
            need this?", and it is the thing a producer checks the agent
            against — so it does not go behind a link. */}
        {line !== undefined && (
          <p className="mt-2 border-l-2 pl-3 text-sm italic text-muted-foreground">
            “{line.quote ?? line.line}”
            {line.scene !== undefined && (
              <span className="not-italic"> — scene {line.scene}</span>
            )}
          </p>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
          <Figure label={`${supplierName(chosen.supplier_id)} offers`}>
            <span className="text-2xl font-semibold">
              {money(chosen.latest_quote?.unit_price)}
            </span>
          </Figure>
          <Figure label="opened at">
            <span className="text-muted-foreground line-through">
              {money(chosen.first_quote?.unit_price)}
            </span>
          </Figure>
          {won !== null && (
            <Figure label="talked down by">
              <span className="font-medium">
                {money(won.amount)} ({String(won.percent)}%)
              </span>
            </Figure>
          )}
          <Figure label="rounds">
            {String(chosen.rounds_used ?? 0)}/{String(chosen.max_rounds ?? "?")}
          </Figure>
          <Figure label="last heard (sim)">{simTime(chosen.last_inbound_at)}</Figure>
        </div>

        {item.reference_band !== undefined && (
          <p className="text-sm text-muted-foreground">
            Research put the market at {money(item.reference_band.low)}–
            {money(item.reference_band.high)}.
          </p>
        )}

        {chosen.latest_reasoning !== undefined && chosen.latest_reasoning !== "" && (
          <p className="text-sm">{chosen.latest_reasoning}</p>
        )}

        {rivals.length > 0 && (
          <>
            <Separator />
            <div className="text-sm">
              <p className="mb-1 text-muted-foreground">Also approached:</p>
              <ul className="space-y-1">
                {rivals.map((r) => (
                  <li key={r.id} className="flex justify-between gap-4">
                    <span>{supplierName(r.supplier_id)}</span>
                    <span className="text-muted-foreground">
                      {money(r.latest_quote?.unit_price)} · {r.state}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}

        {outcome !== null && <Result outcome={outcome} />}
      </CardContent>

      <CardFooter className="flex flex-wrap items-center gap-3">
        <Button
          disabled={busy || outcome?.kind === "approved"}
          onClick={run(() => approve(projectId, item.id, chosen.id))}
        >
          Approve {money(chosen.latest_quote?.unit_price)}
        </Button>
        <Button
          variant="outline"
          disabled={busy}
          onClick={run(() => {
            const price = chosen.latest_quote?.unit_price;
            const floor = {
              amount: Math.round((price?.amount ?? 0) * 0.9),
              currency: price?.currency ?? "MYR",
            };
            return setFloor(projectId, chosen.id, floor);
          })}
        >
          Push for 10% less
        </Button>
        <p className="text-xs text-muted-foreground">
          Approving writes a purchase order through a separate service — the
          agent has no access to it.
        </p>
      </CardFooter>
    </Card>
  );
}

function Figure({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p>{children}</p>
    </div>
  );
}

/** Every outcome says what happened. The duplicate refusal is the one worth
 * reading: it comes from the storage engine, not from our code. */
function Result({ outcome }: { outcome: Outcome }) {
  const tone =
    outcome.kind === "approved"
      ? "border-l-2 border-l-foreground bg-muted"
      : "border-l-2 border-l-destructive bg-muted";
  const text = {
    approved: `Ordered at ${outcome.kind === "approved" ? money(outcome.price) : ""}.`,
    duplicate:
      "Refused — this item already has a purchase order. Firestore rejected " +
      "the write before any of our code ran, because orders are keyed by item.",
    forbidden:
      "Refused — your account has no producer claim, so it may not approve.",
    conflict: "This negotiation moved on since the screen loaded. Refresh.",
    error: "",
  }[outcome.kind];

  return (
    <div className={`rounded-sm p-3 text-sm ${tone}`}>
      <p>{text || ("detail" in outcome ? outcome.detail : "")}</p>
      {"detail" in outcome && text !== "" && outcome.detail !== "" && (
        <p className="mt-1 text-xs text-muted-foreground">{outcome.detail}</p>
      )}
    </div>
  );
}

/**
 * One decision per item, not per negotiation.
 *
 * An item may have three sellers in play and the producer is choosing an item,
 * not a row. The cheapest negotiation at READY_FOR_HUMAN is recommended and
 * the rest are shown as context — which also makes it visible that ordering
 * the same item twice is a thing the system refuses.
 */
