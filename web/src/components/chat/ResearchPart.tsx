/**
 * The agent working, shown while it works.
 *
 * Confirming the props was followed by silence: several ticks of researching,
 * finding sellers and writing first emails, with nothing on screen until the
 * drafts appeared. Working and stuck looked the same, and a producer watching a
 * blank panel reasonably assumes the second.
 *
 * Every line here is a transition that already existed in Firestore. The work
 * was in reading it, which is why the reading lives in `research.ts` with
 * tests, and this file only draws.
 *
 * `status` comes from assistant-ui: a tool-call part carries a
 * ToolCallMessagePartStatus and the library hands it to the renderer. Using it
 * rather than a boolean of our own means the part is running by the same
 * definition everything else in the library uses.
 */

import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { useReducedMotion, motion } from "motion/react";

import type { ResearchItem, Step } from "@/chat/research";

export interface ResearchArgs {
  items?: ResearchItem[];
}

export function ResearchPart({
  args,
  status,
}: ToolCallMessagePartProps<ResearchArgs, unknown>) {
  const items = args.items ?? [];
  if (items.length === 0) return null;
  const running = status.type === "running";

  return (
    <div className="my-2 rounded-lg border px-4 py-3 text-sm">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {running ? "Working" : "Sourcing"}
      </p>
      <p className="mt-1 font-medium">
        {running
          ? `Researching ${String(items.length)} item${items.length === 1 ? "" : "s"}`
          : `${String(items.length)} item${items.length === 1 ? "" : "s"} sourced`}
      </p>
      {running && (
        <p className="mt-1 text-xs text-muted-foreground">
          This runs on the agent's own schedule, a step or two per minute. You do
          not have to wait here.
        </p>
      )}

      <div className="mt-3 space-y-3">
        {items.map((item) => (
          <div key={item.itemId} className="rounded-md border px-3 py-2">
            <p className="font-medium capitalize">{item.name}</p>
            <ol className="mt-1.5 space-y-1">
              {item.steps.map((step) => (
                <StepLine key={step.key} step={step} />
              ))}
            </ol>
          </div>
        ))}
      </div>
    </div>
  );
}

function StepLine({ step }: { step: Step }) {
  return (
    <li className="flex items-start gap-2 text-xs">
      <Marker state={step.state} />
      <div className={step.state === "waiting" ? "text-muted-foreground" : ""}>
        <p>{step.label}</p>
        {step.detail !== undefined && step.detail !== "" && (
          <p className="text-muted-foreground">{step.detail}</p>
        )}
      </div>
    </li>
  );
}

/**
 * Three states, three marks: done, in flight, not started.
 *
 * The running one is the only thing that moves. A list where every row pulses
 * says nothing about which step the agent is actually on, which is the whole
 * question a producer is asking.
 */
function Marker({ state }: { state: Step["state"] }) {
  const still = useReducedMotion();

  if (state === "done") {
    return (
      <span aria-hidden className="mt-0.5 leading-none text-muted-foreground">
        ✓
      </span>
    );
  }
  if (state === "waiting") {
    return (
      <span
        aria-hidden
        className="mt-1.5 size-1.5 shrink-0 rounded-full border border-current opacity-40"
      />
    );
  }
  return (
    <motion.span
      aria-hidden
      className="mt-1.5 size-1.5 shrink-0 rounded-full bg-current"
      animate={still ? undefined : { opacity: [0.3, 1, 0.3] }}
      transition={
        still ? undefined : { duration: 1.2, repeat: Infinity, ease: "easeInOut" }
      }
    />
  );
}
