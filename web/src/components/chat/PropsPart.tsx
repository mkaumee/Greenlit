/**
 * The list the agent read out of a screenplay, for a producer to sign off.
 *
 * This is the gate. Everything found here is a `DRAFT` item — inert, not
 * researched, nobody emailed — until somebody presses Confirm. That gap exists
 * because a model reading a script can produce a prop that was never in it,
 * and the cheapest place to catch that is before it becomes a real message to
 * a real seller.
 *
 * Which is why every row shows the line it came from. A producer is not being
 * asked to trust the list; they are being asked to check it, and the quoted
 * line is what makes checking possible. A prop with no line should never have
 * been reported — see `extract_props` in `contracts/protocols.py`.
 *
 * Quantity is set here, not by the agent, because a prop that breaks on camera
 * needs one per take and only a person knows how many takes the schedule
 * allows.
 */

import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { useState } from "react";

import { confirmProps, type Prop } from "@/chat/api";
import { useProjectId } from "@/components/chat/context";
import { Button } from "@/components/ui/button";

export interface PropsArgs {
  props?: Prop[];
  filename?: string;
}

export function PropsPart({ args }: ToolCallMessagePartProps<PropsArgs, unknown>) {
  const projectId = useProjectId();
  return <PropList projectId={projectId} found={args.props ?? []} name={args.filename ?? ""} />;
}

export function PropList({
  projectId,
  found,
  name,
}: {
  projectId: string;
  found: Prop[];
  name: string;
}) {
  // Everything included by default with the quantity the agent proposed. The
  // producer's job is to catch what is wrong, not to re-enter what is right.
  const [choices, setChoices] = useState<Record<string, { qty: number; include: boolean }>>(
    Object.fromEntries(found.map((p) => [p.item_id, { qty: p.qty, include: true }])),
  );
  const [done, setDone] = useState<string>("");
  const [busy, setBusy] = useState(false);

  if (found.length === 0) {
    return (
      <div className="my-2 rounded-lg border px-4 py-3 text-sm">
        <p className="font-medium">Nothing to buy in {name || "that script"}.</p>
        <p className="mt-1 text-muted-foreground">
          No physical thing a scene needs was found. That is a real answer for a
          few pages of dialogue — and if it looks wrong, the script probably
          arrived as a scan.
        </p>
      </div>
    );
  }

  const kept = found.filter((p) => choices[p.item_id]?.include).length;

  const confirm = () => {
    setBusy(true);
    void confirmProps(
      projectId,
      found.map((p) => ({
        item_id: p.item_id,
        qty: choices[p.item_id]?.qty ?? p.qty,
        include: choices[p.item_id]?.include ?? true,
      })),
    )
      .then((result) => {
        setDone(
          result.kind === "confirmed"
            ? `${result.confirmed.length} prop(s) confirmed. The agent starts researching them on the next tick.`
            : result.detail,
        );
      })
      .finally(() => setBusy(false));
  };

  return (
    <div className="my-2 rounded-lg border px-4 py-3 text-sm">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Read from {name || "the script"}
      </p>
      <p className="mt-1 font-medium">
        {found.length} prop{found.length === 1 ? "" : "s"} found
      </p>

      <div className="mt-3 space-y-2">
        {found.map((prop) => {
          const choice = choices[prop.item_id] ?? { qty: prop.qty, include: true };
          return (
            <div
              key={prop.item_id}
              className={`rounded-md border px-3 py-2 ${choice.include ? "" : "opacity-50"}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="checkbox"
                  checked={choice.include}
                  disabled={done !== ""}
                  onChange={(e) =>
                    setChoices((prior) => ({
                      ...prior,
                      [prop.item_id]: { ...choice, include: e.target.checked },
                    }))
                  }
                />
                <span className="font-medium capitalize">{prop.name}</span>
                {prop.consumable && (
                  <span className="rounded-sm bg-muted px-1.5 py-0.5 text-xs">
                    destroyed on camera
                  </span>
                )}
                <label className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
                  qty
                  <input
                    type="number"
                    min={1}
                    value={choice.qty}
                    disabled={done !== "" || !choice.include}
                    onChange={(e) =>
                      setChoices((prior) => ({
                        ...prior,
                        [prop.item_id]: {
                          ...choice,
                          qty: Math.max(1, Number(e.target.value) || 1),
                        },
                      }))
                    }
                    className="w-14 rounded border bg-background px-1 py-0.5"
                  />
                </label>
              </div>
              {prop.lines[0] !== undefined && (
                // The receipt. Not behind a link: the research on agent
                // interfaces is consistent that people say citations raise
                // their confidence and then never click one.
                <p className="mt-1 border-l-2 pl-2 text-xs text-muted-foreground italic">
                  “{prop.lines[0]}”
                </p>
              )}
            </div>
          );
        })}
      </div>

      {done === "" ? (
        <div className="mt-3 flex items-center gap-3">
          <Button size="sm" disabled={busy || kept === 0} onClick={confirm}>
            {busy ? "Confirming…" : `Confirm ${kept}`}
          </Button>
          <span className="text-xs text-muted-foreground">
            Nothing is researched or emailed until you do.
          </span>
        </div>
      ) : (
        <p className="mt-3 font-medium">{done}</p>
      )}
    </div>
  );
}
