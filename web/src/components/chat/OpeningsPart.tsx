/**
 * The first emails, before they go.
 *
 * The prop list is the gate on what gets bought. This is the gate on what gets
 * *said*, and until now there wasn't one: the tick researched an item, opened
 * a negotiation and mailed a stranger on the next pass — a message written by
 * a model, sent from the producer's own mailbox, over their name, and read by
 * them afterwards if at all.
 *
 * So they are shown, and they are editable. Editable is the part that matters:
 * a producer who can only approve or refuse will approve, because refusing
 * costs them the whole negotiation. Being able to fix one sentence is what
 * makes reading them worth doing.
 *
 * Approved as a batch, with one button, for the same reason the props are: a
 * column of separate Send buttons invites sending half of them and leaves the
 * rest in a state nobody is coming back to.
 */

import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { useState } from "react";

import { releaseOpenings, editOpening } from "@/chat/api";
import type { PendingOpening } from "@/chat/rows";
import { useProjectId } from "@/components/chat/context";
import { Button } from "@/components/ui/button";

export interface OpeningsArgs {
  openings?: PendingOpening[];
}

export function OpeningsPart({
  args,
}: ToolCallMessagePartProps<OpeningsArgs, unknown>) {
  const projectId = useProjectId();
  return <Openings projectId={projectId} pending={args.openings ?? []} />;
}

export function Openings({
  projectId,
  pending,
}: {
  projectId: string;
  pending: PendingOpening[];
}) {
  const [edits, setEdits] = useState<Record<string, { subject: string; body: string }>>(
    {},
  );
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const [error, setError] = useState("");

  if (pending.length === 0) return null;

  const textOf = (opening: PendingOpening) =>
    edits[opening.negotiationId] ?? {
      subject: opening.subject,
      body: opening.body,
    };

  const send = () => {
    setBusy(true);
    setError("");
    // Saved before approving, and awaited: approving makes the row due, and a
    // tick can pick it up within the minute. An edit still in flight at that
    // moment would be a rewrite the supplier never sees.
    void (async () => {
      try {
        for (const opening of pending) {
          const edited = edits[opening.negotiationId];
          if (edited === undefined) continue;
          const saved = await editOpening(
            projectId,
            opening.negotiationId,
            edited.subject,
            edited.body,
          );
          if (saved.kind === "error") {
            setError(saved.detail);
            return;
          }
        }
        const released = await releaseOpenings(
          projectId,
          pending.map((o) => o.negotiationId),
        );
        if (released.kind === "error") {
          setError(released.detail);
          return;
        }
        setDone(
          `${pending.length} email${pending.length === 1 ? "" : "s"} on the way. ` +
            "The agent takes it from here and answers the replies itself.",
        );
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div className="my-2 rounded-lg border px-4 py-3 text-sm">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Waiting to be sent
      </p>
      <p className="mt-1 font-medium">
        {pending.length} opening email{pending.length === 1 ? "" : "s"}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Written by the agent, from your mailbox, over your name. Nothing has
        gone yet — read them, change anything you like, then send.
      </p>

      <div className="mt-3 space-y-3">
        {pending.map((opening) => {
          const text = textOf(opening);
          const change = (next: Partial<{ subject: string; body: string }>) =>
            setEdits((prior) => ({
              ...prior,
              [opening.negotiationId]: { ...text, ...next },
            }));
          return (
            <div key={opening.negotiationId} className="rounded-md border px-3 py-2">
              <p className="text-xs text-muted-foreground">
                To <span className="font-medium">{opening.supplier}</span> about{" "}
                <span className="font-medium capitalize">{opening.itemName}</span>
              </p>
              <input
                value={text.subject}
                disabled={busy || done !== ""}
                onChange={(e) => change({ subject: e.target.value })}
                className="mt-2 w-full rounded border bg-background px-2 py-1 text-sm font-medium"
              />
              <textarea
                value={text.body}
                rows={7}
                disabled={busy || done !== ""}
                onChange={(e) => change({ body: e.target.value })}
                className="mt-2 w-full resize-y rounded border bg-background px-2 py-1 font-mono text-xs"
              />
            </div>
          );
        })}
      </div>

      {done === "" ? (
        <div className="mt-3 flex items-center gap-3">
          <Button size="sm" loading={busy} onClick={send}>
            {busy ? "Sending…" : `Send ${pending.length}`}
          </Button>
          <span className="text-xs text-muted-foreground">
            Nothing reaches a seller until you do.
          </span>
        </div>
      ) : (
        <p className="mt-3 font-medium">{done}</p>
      )}
      {error !== "" && <p className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  );
}
