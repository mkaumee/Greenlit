/**
 * The front door.
 *
 * Three columns, and the middle one is a conversation only some of the time.
 * Most of what scrolls past is the agent working: emails to suppliers, replies
 * that came back days later, and the point at which it stopped and asked. The
 * producer can type into it, but nothing here depends on them doing so — the
 * screen fills itself while nobody is watching, which is the claim this
 * product makes and the reason the transcript is the home screen rather than a
 * table of statuses.
 *
 * ## The decision appears twice, deliberately
 *
 * Once in the transcript, where it happened, and once in the rail, which does
 * not scroll. The duplication is the feature: a purchase a producer has to
 * scroll back to find is a purchase they will miss, and `READY_FOR_HUMAN` is
 * the one thing in this system that cannot be missed. Both render the same
 * component from the same row, so they cannot disagree.
 *
 * The right-hand inspector is the earlier panel — `Inbox`, `Breakdown`,
 * `Savings` — moved rather than rewritten. They already take plain rows and
 * are already the place a producer checks the agent's evidence.
 */

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { Row } from "@/chat/rows";
import { useGreenlitThread } from "@/chat/useGreenlitThread";
import { Decision } from "@/components/chat/DecisionPart";
import { signOutOfEverything, USE_EMULATOR, type User } from "@/firebase";
import { MailboxCard } from "@/components/chat/MailboxCard";
import { NewProduction } from "@/components/chat/NewProduction";
import { PendingProject } from "@/components/chat/context";
import { Transcript } from "@/components/chat/Transcript";
import { Skeleton, SkeletonRows } from "@/components/ui/skeleton";
import type { Item, Negotiation, Supplier } from "@/hooks/useProject";
import { useAllMessages, useMailbox } from "@/hooks/useProject";
import { Breakdown } from "@/screens/Breakdown";
import { Inbox } from "@/screens/Inbox";
import { Savings } from "@/screens/Savings";

type Panel = "needs-you" | "breakdown" | "savings";

export function Chat({
  user,
  projectId,
  projectIds,
  onPickProject,
  items,
  negotiations,
  suppliers,
  supplierName,
  readError,
  loading,
}: {
  user: User;
  projectId: string;
  projectIds: string[];
  onPickProject: (id: string) => void;
  items: Item[];
  negotiations: Negotiation[];
  suppliers: Supplier[];
  supplierName: (id: string | undefined) => string;
  /** Whatever Firestore refused, if anything. Empty when all reads are fine. */
  readError: string;
  /** True while the reads behind every panel on this screen are still open. */
  loading: boolean;
}) {
  const mailbox = useMailbox();
  const [panel, setPanel] = useState<Panel>("needs-you");

  const negotiationIds = useMemo(
    () => negotiations.map((n) => n.id),
    [negotiations],
  );
  const { rows: messages, loading: messagesLoading } = useAllMessages(
    projectId,
    negotiationIds,
  );

  const { runtime, waiting, readScript, busy } = useGreenlitThread({
    projectId,
    items,
    negotiations,
    suppliers,
    messages,
  });

  return (
    <PendingProject value={projectId}>
      <AssistantRuntimeProvider runtime={runtime}>
        {/* `min-h-0` on every column, not decoration: a grid child defaults to
            `min-height:auto`, so a tall transcript grows the row instead of
            scrolling inside it — which pushes the composer off the bottom of
            the window and leaves no way to type. */}
        <div className="grid h-screen grid-cols-[16rem_1fr_24rem] divide-x overflow-hidden">
          <Rail
            user={user}
            projectId={projectId}
            projectIds={projectIds}
            onPickProject={onPickProject}
            mailbox={mailbox}
            waiting={waiting}
            readError={readError}
            loading={loading}
          />

          <div className="flex min-h-0 flex-col">
            <Transcript
              busy={busy}
              // Not the same `loading` the panels get. The transcript is empty
              // for longer: its rows come from the correspondence, which is a
              // subscription per negotiation and cannot even start until the
              // negotiations themselves have arrived.
              loading={loading || messagesLoading}
              onScript={(file) => void readScript(file)}
            />
          </div>

          <aside className="flex min-h-0 flex-col overflow-hidden">
            <nav className="flex gap-1 border-b px-4">
              <Switch now={panel} to="needs-you" set={setPanel} label="Needs you" />
              <Switch now={panel} to="breakdown" set={setPanel} label="Breakdown" />
              <Switch now={panel} to="savings" set={setPanel} label="Savings" />
            </nav>
            <div className="flex-1 overflow-y-auto p-4">
              {panel === "needs-you" && (
                <Inbox
                  projectId={projectId}
                  items={items}
                  negotiations={negotiations}
                  supplierName={supplierName}
                  loading={loading}
                />
              )}
              {panel === "breakdown" && (
                <Breakdown
                  items={items}
                  negotiations={negotiations}
                  loading={loading}
                />
              )}
              {panel === "savings" && (
                <Savings items={items} negotiations={negotiations} loading={loading} />
              )}
            </div>
          </aside>
        </div>
      </AssistantRuntimeProvider>
    </PendingProject>
  );
}

function Rail({
  user,
  projectId,
  projectIds,
  onPickProject,
  mailbox,
  waiting,
  readError,
  loading,
}: {
  user: User;
  projectId: string;
  projectIds: string[];
  onPickProject: (id: string) => void;
  mailbox: ReturnType<typeof useMailbox>;
  waiting: Row[];
  readError: string;
  loading: boolean;
}) {
  return (
    <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
      <div className="flex items-center gap-2">
        <h1 className="font-semibold">Greenlit</h1>
        {USE_EMULATOR && (
          <span className="rounded-sm bg-muted px-1.5 py-0.5 text-xs">emulator</span>
        )}
      </div>

      <div>
        <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Production
        </p>
        {projectIds.length > 1 ? (
          <select
            className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-sm"
            onChange={(e) => onPickProject(e.target.value)}
            value={projectId}
          >
            {projectIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        ) : loading ? (
          // "none yet" is what a producer who owns three productions saw on
          // the way in, for as long as the query took.
          <Skeleton className="mt-1 h-5 w-32" />
        ) : (
          <p className="mt-1 text-sm break-all">{projectId || "none yet"}</p>
        )}
      </div>

      {/* A refused read is why an empty screen and a broken one look the same.
          Said once, at the top, because every collection on this screen shares
          one cause when it happens: rules deployed behind the code. */}
      {readError !== "" && (
        <div className="rounded-lg border border-destructive/50 p-3 text-xs">
          <p className="font-medium">Firestore refused a read</p>
          <p className="mt-1 text-muted-foreground">
            What is on this screen may be incomplete rather than empty. Usually
            the deployed security rules are behind the code —{" "}
            <code>make deploy-rules</code> pushes them. {readError}
          </p>
        </div>
      )}

      <MailboxCard state={mailbox} />

      <NewProduction onStarted={onPickProject} />

      <div>
        <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Needs you
          {waiting.length > 0 && (
            <span className="ml-2 rounded-full bg-foreground px-1.5 py-0.5 text-xs text-background">
              {waiting.length}
            </span>
          )}
        </p>
        {loading ? (
          <SkeletonRows rows={2} className="mt-1" />
        ) : waiting.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            Nothing is waiting on a decision. The agent stops here every time,
            so this filling in is how you find out.
          </p>
        ) : (
          <div className="mt-1 space-y-2">
            {waiting.map((row) =>
              row.kind === "decision" ? (
                <Decision
                  key={row.id}
                  compact
                  args={{
                    itemId: row.itemId,
                    item: row.itemName,
                    supplier: row.supplier,
                    price: row.price,
                    rounds: row.roundsUsed,
                    reason: row.reason,
                    rivals: row.rivals,
                  }}
                  negotiationId={row.negotiationId}
                  projectId={projectId}
                />
              ) : null,
            )}
          </div>
        )}
      </div>

      {/* Pushed to the bottom: the account, a way out, and the older screens.
          They are reachable from here rather than deleted — the inspector on
          the right holds the same three, and until this screen has been lived
          with for a while, losing a working panel to a new layout would be a
          bad trade. */}
      <div className="mt-auto space-y-1 border-t pt-3 text-xs text-muted-foreground">
        <p className="break-all">{user.email ?? user.uid}</p>
        <div className="flex gap-3">
          <Link className="underline-offset-4 hover:underline" to="/inbox">
            Panels
          </Link>
          <button
            type="button"
            className="underline-offset-4 hover:underline"
            onClick={() => void signOutOfEverything()}
          >
            Sign out
          </button>
        </div>
      </div>
    </aside>
  );
}

function Switch({
  now,
  to,
  set,
  label,
}: {
  now: Panel;
  to: Panel;
  set: (p: Panel) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={() => set(to)}
      className={`-mb-px border-b-2 px-3 py-2 text-sm ${
        now === to
          ? "border-foreground font-medium"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}
