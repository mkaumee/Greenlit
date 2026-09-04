/**
 * One spinner, so five buttons cannot disagree about what "working" looks like.
 *
 * Sized in `em` rather than pixels: it sits inside buttons that range from
 * `size="xs"` to `size="lg"`, and inheriting the font size means it is always
 * proportionate to the label beside it without a variant of its own.
 *
 * Stops entirely under `prefers-reduced-motion`, and does not become invisible
 * when it does — a ring that no longer turns still says "there is something
 * here", and the label beside it is doing the real work of saying what.
 *
 * Decorative to a screen reader, deliberately. Every place this appears has
 * words beside it — "Starting…", "Checking sign-in…" — and the button it sits
 * in carries `aria-busy`. A `role="status"` here as well would announce the
 * same wait twice, once as a label nobody wrote.
 */

import { useReducedMotion, motion } from "motion/react";

import { cn } from "@/lib/utils";

export function Spinner({ className }: { className?: string }) {
  const still = useReducedMotion();

  return (
    <motion.span
      aria-hidden
      className={cn(
        "inline-block size-[1em] shrink-0 rounded-full border-2 border-current",
        // The gap in the ring is what makes rotation visible at all. Kept when
        // motion is off too: a complete circle reads as a full-stop dot.
        "border-r-transparent",
        className,
      )}
      animate={still ? undefined : { rotate: 360 }}
      transition={
        still ? undefined : { duration: 0.7, repeat: Infinity, ease: "linear" }
      }
    />
  );
}
