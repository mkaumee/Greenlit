/**
 * A placeholder shaped like the thing that is coming.
 *
 * Used where an empty state would otherwise be a lie. `Breakdown` said "No
 * items yet" while the Firestore read was still in flight, so a production
 * that was loading and one that was genuinely empty looked identical — the
 * same failure the mailbox card had when it offered Connect Gmail to an
 * already-connected account.
 *
 * Shaped rather than a spinner because the shape is the information: three
 * bars where three rows are about to be tells you what to expect, and stops
 * the layout jumping when they arrive.
 */

import { useReducedMotion, motion } from "motion/react";

import { cn } from "@/lib/utils";

export function Skeleton({
  className,
  delay = 0,
}: {
  className?: string;
  /** Offsets the sweep, so a stack of these reads as one movement. */
  delay?: number;
}) {
  const still = useReducedMotion();

  return (
    <div
      aria-hidden
      // `overflow-hidden` is what makes this a sweep rather than a bar sliding
      // across the page: the highlight has to be clipped to the placeholder it
      // belongs to.
      //
      // Not `bg-muted`, which is the obvious token and the wrong one here. It
      // is oklch(0.97) against a white page and the highlight is white, which
      // is three percent of contrast — the sweep was running correctly and was
      // invisible, which is indistinguishable from it not running at all.
      // `foreground/10` lands near #e9e9e9 and gives the highlight something to
      // travel across.
      className={cn(
        "relative overflow-hidden rounded-md bg-foreground/10",
        className,
      )}
    >
      {!still && (
        <motion.div
          className="absolute inset-y-0 w-1/2"
          style={{
            // A literal gradient rather than Tailwind's, because v4 renamed
            // `bg-gradient-to-r` to `bg-linear-to-r` and the old name is a
            // compatibility alias. Writing the CSS is one line and cannot go
            // quietly transparent on an upgrade.
            background:
              "linear-gradient(90deg, transparent, var(--background), transparent)",
          }}
          // The child is half the parent's width, and `x` is a percentage of
          // the *child*. So -100% puts its right edge at the left edge of the
          // placeholder, and 200% puts its left edge past the right one:
          // fully off, travel across, fully off.
          animate={{ x: ["-100%", "200%"] }}
          transition={{
            duration: 1.5,
            repeat: Infinity,
            ease: "linear",
            // A beat between passes. Without it the highlight re-enters the
            // instant it leaves, which reads as a flicker rather than a sweep.
            repeatDelay: 0.35,
            delay,
          }}
        />
      )}
    </div>
  );
}

/**
 * The usual case: a few placeholder rows while a collection loads.
 *
 * `aria-hidden` on each bar and one live region here, so a screen reader is
 * told "loading" once rather than read a fence of empty divs.
 *
 * The rows are offset from each other so the light travels down the stack
 * instead of all of them flashing in unison.
 */
export function SkeletonRows({
  rows = 3,
  className,
}: {
  rows?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-16 w-full" delay={i * 0.12} />
      ))}
    </div>
  );
}
