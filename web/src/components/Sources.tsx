/**
 * Where a price band's numbers came from — or that nobody knows.
 *
 * The claim this system makes is that it keeps the URLs it got its numbers
 * from. Those URLs have been in Firestore since research first ran and were
 * never on screen, because the panel's own ReferenceBand type declared only
 * the range. A band researched from three sources and one the model
 * remembered looked identical. This is the difference, rendered.
 *
 * The empty case is the one that matters and is deliberately not silent. A
 * range with nothing under it is a number nobody can check, and it should cost
 * the reader's confidence rather than borrow it.
 */

import { hostOf, sourceLabel } from "@/lib/sources";

export function Sources({ urls }: { urls: string[] | undefined }) {
  const missing = sourceLabel(urls);
  if (missing !== null || urls === undefined) {
    return (
      <span className="text-xs text-destructive" title="Nothing backs this range">
        {missing ?? "no sources"}
      </span>
    );
  }

  return (
    <span className="flex flex-wrap gap-x-2 text-xs">
      {urls.map((url) => (
        <a
          key={url}
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          className="underline-offset-2 hover:underline"
          title={url}
        >
          {hostOf(url)}
        </a>
      ))}
    </span>
  );
}
