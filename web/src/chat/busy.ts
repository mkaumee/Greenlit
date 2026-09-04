/**
 * What the agent is doing right now, and what to call it on screen.
 *
 * A boolean was not enough. `isRunning` is true for two waits that feel
 * nothing alike: answering a question takes a second or two, and reading a
 * screenplay takes twenty and up — an upload, a Gemini call over a whole
 * document, and a list of props coming back. A spinner that says the same
 * thing for both leaves a producer thirty seconds into the long one wondering
 * whether it is stuck.
 *
 * So the state carries what is being waited on, and the label names it. The
 * filename is in there because *"Reading kopitiam-nights.txt…"* is the
 * sentence that makes a long wait legible: it is doing the thing you asked,
 * to the file you gave it.
 *
 * Pure, and here rather than in the component, for the same reason `rows.ts`
 * and `mailbox.ts` are: `web/tests` has no jsdom and no testing-library, so
 * anything living inside a component is verified by looking at it. The copy is
 * the part that can be held to a test, so the copy lives out here.
 */

/** Nothing in flight. The thread is idle and the composer is live. */
export interface Idle {
  kind: "idle";
}

/** A producer asked something and `/chat` has not answered yet. */
export interface Asking {
  kind: "asking";
}

/** A screenplay is on its way to the brain. The long one. */
export interface Reading {
  kind: "reading";
  filename: string;
}

export type Busy = Idle | Asking | Reading;

export const IDLE: Busy = { kind: "idle" };

/** True while anything is in flight. What assistant-ui's `isRunning` reads. */
export const isBusy = (busy: Busy): boolean => busy.kind !== "idle";

/**
 * One line, written for the person waiting.
 *
 * Empty when idle, so a caller that renders it unconditionally shows nothing
 * rather than the word "idle".
 */
export function busyLabel(busy: Busy): string {
  switch (busy.kind) {
    case "idle":
      return "";
    case "asking":
      return "Thinking…";
    case "reading":
      return busy.filename === ""
        ? "Reading the screenplay…"
        : `Reading ${busy.filename}…`;
  }
}

/**
 * The second line, or empty when there is nothing worth adding.
 *
 * Only the long wait gets one. Saying "this takes a moment" under a two-second
 * spinner is noise; saying it under a thirty-second one is the difference
 * between waiting and reloading the page.
 */
export function busyDetail(busy: Busy): string {
  if (busy.kind !== "reading") return "";
  return "Every page, then every physical thing a scene needs. This takes a moment.";
}
