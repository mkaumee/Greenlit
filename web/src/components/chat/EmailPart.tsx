/**
 * One real email in the transcript, in or out.
 *
 * Most of what appears in this thread is this: correspondence with suppliers
 * that happened while nobody was watching, drawn out of Firestore in the order
 * it occurred. A producer scrolling back is reading the actual record — the
 * same append-only `messages` subcollection the tick wrote, not a summary of
 * it — which is the difference between claiming the agent negotiated for five
 * days and showing it.
 *
 * Inbound and outbound are told apart by more than a label: a reply from a
 * seller is the event worth noticing, so it gets the border and the emphasis.
 */

import type { ToolCallMessagePartProps } from "@assistant-ui/react";

export interface EmailArgs {
  direction?: "inbound" | "outbound";
  supplier?: string;
  item?: string;
  subject?: string;
}

export interface EmailResult {
  body?: string;
}

export function EmailPart({
  args,
  result,
}: ToolCallMessagePartProps<EmailArgs, EmailResult>) {
  const inbound = args.direction === "inbound";
  const body = result?.body ?? "";

  return (
    <div
      className={`my-2 rounded-lg border px-4 py-3 text-sm ${
        inbound ? "border-foreground/30 bg-muted/50" : "bg-background"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {inbound ? "reply from" : "sent to"}
        </span>
        <span className="font-medium">{args.supplier ?? "a seller"}</span>
        {args.item !== undefined && args.item !== "" && (
          <span className="text-muted-foreground">about {args.item}</span>
        )}
      </div>
      {args.subject !== undefined && args.subject !== "" && (
        <p className="mt-1 font-medium">{args.subject}</p>
      )}
      {body !== "" && (
        // Preserved as sent. Reflowing a supplier's email would make the
        // transcript a paraphrase of the evidence rather than the evidence.
        <p className="mt-2 whitespace-pre-wrap text-muted-foreground">{body}</p>
      )}
    </div>
  );
}
