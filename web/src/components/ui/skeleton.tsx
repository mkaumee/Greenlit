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

export function Skeleton({ className }: { className?: string }) {
  const still = useReducedMotion();

  return (
    <motion.div
      aria-hidden
      className={cn("rounded-md bg-muted", className)}
      animate={still ? undefined : { opacity: [0.45, 0.9, 0.45] }}
      transition={
        still
          ? undefined
          : { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
      }
    />
  );
}

/**
 * The usual case: a few placeholder rows while a collection loads.
 *
 * `aria-hidden` on each bar and one live region here, so a screen reader is
 * told "loading" once rather than read a fence of empty divs.
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
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
  );
}
