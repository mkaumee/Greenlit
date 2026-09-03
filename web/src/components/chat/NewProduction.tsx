/**
 * Starting a production, which until now needed a shell and a gcloud token.
 *
 * Owned by whoever presses it — the API takes no owner field, it reads the
 * caller's token — so there is no way to create a production in somebody
 * else's name, and no way to create one nobody owns. An unowned production is
 * invisible to every browser, which is the safe default and was also the bug
 * that left this panel empty for a day.
 */

import { useState } from "react";

import { startProject } from "@/chat/api";
import { Button } from "@/components/ui/button";

export function NewProduction({ onStarted }: { onStarted: (id: string) => void }) {
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const start = () => {
    const named = title.trim();
    if (named === "") return;
    setBusy(true);
    setError("");
    void startProject(named)
      .then((result) => {
        if (result.kind === "error") {
          setError(result.detail);
          return;
        }
        setTitle("");
        onStarted(result.projectId);
      })
      .finally(() => setBusy(false));
  };

  return (
    <div className="rounded-lg border p-3 text-sm">
      <p className="font-medium">New production</p>
      <input
        value={title}
        disabled={busy}
        placeholder="Nasi Lemak Nights"
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") start();
        }}
        className="mt-2 w-full rounded-md border bg-background px-2 py-1 text-sm"
      />
      <Button
        size="sm"
        className="mt-2"
        disabled={busy || title.trim() === ""}
        onClick={start}
      >
        {busy ? "Starting…" : "Start"}
      </Button>
      {error !== "" && <p className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  );
}
