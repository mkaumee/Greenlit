/**
 * The agent, mid-thought, on screen.
 *
 * This is the gap that made the product look broken. Drop a screenplay on the
 * thread and the transcript showed `Read kopitiam-nights.txt.` and then
 * nothing at all — for the upload, a Gemini call over the whole document, and
 * a list of props coming back. Twenty seconds and up, with no way to tell a
 * working system from a dead one. `isRunning` was already true on the runtime
 * the whole time; nothing rendered it.
 *
 * Visibility comes from `useAuiState((s) => s.thread.isRunning)` — the
 * runtime's own idea of whether it is working, which is also what disables the
 * composer, so the indicator and the input cannot disagree about the state of
 * the thread. (`ThreadPrimitive.If running` says the same thing; this version
 * of assistant-ui deprecates it in favour of reading the state directly, the
 * same swap the tool renderers made when `makeAssistantToolUI` went.)
 *
 * The hook rather than the `AuiIf` component specifically so this stays
 * mounted across the transition. `AuiIf` returns null the instant the thread
 * stops, which tears the indicator out before `AnimatePresence` can play it
 * out — an exit animation that never runs is worse than none, because writing
 * one reads as having handled the case.
 *
 * `busy` supplies the words. It is the same state `isRunning` is derived from,
 * so the two cannot disagree; what it adds is *which* wait this is.
 */

import { useAuiState } from "@assistant-ui/react";
import { AnimatePresence, useReducedMotion, motion } from "motion/react";

import { busyDetail, busyLabel, type Busy } from "@/chat/busy";

export function Working({ busy }: { busy: Busy }) {
  const running = useAuiState((s) => s.thread.isRunning);
  const label = busyLabel(busy);
  const detail = busyDetail(busy);

  return (
    <AnimatePresence>
      {running && label !== "" && (
        <motion.div
          key="working"
          className="my-3 flex items-start gap-2 text-sm text-muted-foreground"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          // Announced once, and politely: this changes while a producer may be
          // reading something else on the page.
          role="status"
          aria-live="polite"
        >
          <Dots />
          <div>
            <p>{label}</p>
            {detail !== "" && <p className="mt-0.5 text-xs">{detail}</p>}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * Three dots, staggered.
 *
 * The one piece of motion here that is doing a job rather than decorating: a
 * static row of dots and an animated one are the difference between "it is
 * thinking" and "it stopped there". Under `prefers-reduced-motion` they hold
 * still and the label carries the meaning on its own.
 */
function Dots() {
  const still = useReducedMotion();

  return (
    <span className="mt-1.5 flex shrink-0 gap-1" aria-hidden>
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="size-1.5 rounded-full bg-current"
          animate={still ? undefined : { opacity: [0.25, 1, 0.25] }}
          transition={
            still
              ? undefined
              : {
                  duration: 1.1,
                  repeat: Infinity,
                  ease: "easeInOut",
                  // The stagger is what reads as travel rather than a blink.
                  delay: i * 0.16,
                }
          }
        />
      ))}
    </span>
  );
}
