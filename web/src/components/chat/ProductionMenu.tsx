/**
 * Renaming a production, and getting rid of one.
 *
 * A producer could start a production and never be free of it: mistype the
 * title, start the wrong thing, run the demo twice, and it sat in the rail for
 * good. Both of those are small mistakes that deserve small remedies.
 *
 * The two are not symmetrical, and the screen should not pretend they are.
 * Renaming touches one field and is undone by renaming again, so it happens
 * inline. Deleting takes the props, the negotiations and every email with it
 * and cannot be undone, so it asks first — and the asking says what actually
 * goes, including the part a producer cannot see: that suppliers already
 * written to will never hear back.
 */

import { useState } from "react";

import { deleteProject, renameProject } from "@/chat/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function ProductionMenu({
  projectId,
  title,
  live,
  onDeleted,
}: {
  projectId: string;
  /** What it is called now. The id is what it is filed under, and never moves. */
  title: string;
  /** How many negotiations have already written to a supplier. */
  live: number;
  onDeleted: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(title);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (projectId === "") return null;

  const rename = () => {
    const named = draft.trim();
    if (named === "" || named === title) {
      setRenaming(false);
      return;
    }
    setBusy(true);
    setError("");
    void renameProject(projectId, named)
      .then((result) => {
        if (result.kind === "error") {
          setError(result.detail);
          return;
        }
        // Nothing to set locally: the snapshot listener on `projects` brings
        // the new title back on its own, which is also the proof it saved.
        setRenaming(false);
      })
      .finally(() => setBusy(false));
  };

  const remove = () => {
    setBusy(true);
    setError("");
    void deleteProject(projectId)
      .then((result) => {
        if (result.kind === "error") {
          setError(result.detail);
          return;
        }
        setAsking(false);
        onDeleted();
      })
      .finally(() => setBusy(false));
  };

  if (renaming) {
    return (
      <div className="mt-1 space-y-1">
        <input
          autoFocus
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") rename();
            if (e.key === "Escape") setRenaming(false);
          }}
          className="w-full rounded-md border bg-background px-2 py-1 text-sm"
        />
        <div className="flex gap-2">
          <Button size="xs" loading={busy} onClick={rename}>
            Save
          </Button>
          <Button
            size="xs"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setDraft(title);
              setRenaming(false);
            }}
          >
            Cancel
          </Button>
        </div>
        {error !== "" && <p className="text-xs text-destructive">{error}</p>}
      </div>
    );
  }

  return (
    <>
      <div className="mt-1 flex gap-3 text-xs text-muted-foreground">
        <button
          type="button"
          className="underline-offset-4 hover:underline"
          onClick={() => {
            setDraft(title);
            setRenaming(true);
          }}
        >
          Rename
        </button>
        <button
          type="button"
          className="underline-offset-4 hover:underline hover:text-destructive"
          onClick={() => {
            setError("");
            setAsking(true);
          }}
        >
          Delete
        </button>
      </div>

      <Dialog open={asking} onOpenChange={setAsking}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {title || projectId}?</DialogTitle>
            <DialogDescription asChild>
              <div className="space-y-2 text-left">
                <p>
                  This removes the production, every prop read out of its
                  script, every negotiation, and all of the correspondence.
                  There is no undo.
                </p>
                {live > 0 && (
                  <p className="font-medium text-destructive">
                    {live} supplier{live === 1 ? " has" : "s have"} already been
                    emailed about this production. Deleting it means the agent
                    stops mid-conversation and they never hear back.
                  </p>
                )}
                <p>
                  Anything already approved stays: a purchase order outlives the
                  production it was approved for, because the money did.
                </p>
              </div>
            </DialogDescription>
          </DialogHeader>
          {error !== "" && <p className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => setAsking(false)}
            >
              Keep it
            </Button>
            <Button variant="destructive" loading={busy} onClick={remove}>
              {busy ? "Deleting…" : "Delete for good"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
