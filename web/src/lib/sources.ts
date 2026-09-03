/**
 * The pure half of rendering a band's sources.
 *
 * Split out so it can be tested without a DOM: `Sources.tsx` is markup around
 * these two decisions, and the decisions are where a mistake would be silent —
 * a band with no sources rendering as though it had some is exactly the
 * failure the feature exists to expose.
 */

/**
 * How a source URL is labelled. The hostname, because a research URL is a
 * paragraph wide and the point is which publication, not which page.
 *
 * A value that will not parse comes back as-is rather than being dropped: a
 * malformed source is still evidence about the model that produced it, and
 * hiding it would make the row look cleaner than the data is.
 */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 30);
  }
}

/** What to say about a band's sources. `null` means render the links. */
export function sourceLabel(urls: string[] | undefined): string | null {
  return urls === undefined || urls.length === 0 ? "no sources" : null;
}
