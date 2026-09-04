/**
 * The thread itself: what happened, oldest first, and a box to ask about it.
 *
 * Built on assistant-ui's headless primitives rather than its packaged Thread
 * component, because most of what is in here is not a conversation. Two of the
 * four row kinds are the agent working — emails it sent, replies it got, drawn
 * live out of Firestore while nobody is typing — and they are the point. A
 * chat UI that assumed every message was said to somebody would have to hide
 * them or fake them.
 *
 * Tool renderers are passed to `MessagePrimitive.Parts` rather than registered
 * with `makeAssistantToolUI`, which this version of the library deprecates in
 * favour of exactly this. The names come from `convert.ts`, so the side that
 * emits a part and the side that renders it cannot drift apart into a message
 * with no body.
 */

import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
} from "@assistant-ui/react";
import { useReducedMotion, motion } from "motion/react";
import { useRef, useState } from "react";

import type { Busy } from "@/chat/busy";
import { DECISION_TOOL, EMAIL_TOOL, PROPS_TOOL } from "@/chat/convert";
import { DecisionPart } from "@/components/chat/DecisionPart";
import { EmailPart } from "@/components/chat/EmailPart";
import { PropsPart } from "@/components/chat/PropsPart";
import { Working } from "@/components/chat/Working";

const TOOLS = {
  tools: {
    by_name: {
      [EMAIL_TOOL]: EmailPart,
      [DECISION_TOOL]: DecisionPart,
      [PROPS_TOOL]: PropsPart,
    },
  },
} as const;

export function Transcript({
  onScript,
  busy,
}: {
  /** A screenplay dropped anywhere on the thread. */
  onScript: (file: File) => void;
  /** What is in flight, for the indicator below the last row. */
  busy: Busy;
}) {
  const [over, setOver] = useState(false);

  return (
    <ThreadPrimitive.Root
      className={`flex h-full flex-col ${over ? "bg-muted/40" : ""}`}
      onDragOver={(e) => {
        // Both handlers, and both preventDefault: without one on dragover the
        // browser treats the drop as a navigation and opens the PDF in the tab,
        // losing whatever was on screen.
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const file = e.dataTransfer.files[0];
        if (file) onScript(file);
      }}
    >
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-6 py-4">
        <ThreadPrimitive.Empty>
          <Empty />
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <Producer /> : <Agent />
          }
        </ThreadPrimitive.Messages>

        {/* Always mounted; it decides for itself whether to show, by reading
            the runtime's `isRunning`. Gating it from out here would unmount it
            the instant the thread stopped and kill its exit animation. */}
        <Working busy={busy} />
      </ThreadPrimitive.Viewport>

      <div className="border-t px-6 py-4">
        <ComposerPrimitive.Root className="flex items-end gap-2">
          <ComposerPrimitive.Input
            autoFocus
            rows={1}
            placeholder="Ask what needs you, who has gone quiet, what things cost…"
            className="max-h-40 flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <ScriptButton onScript={onScript} />
          <ComposerPrimitive.Send className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            Ask
          </ComposerPrimitive.Send>
        </ComposerPrimitive.Root>
      </div>
    </ThreadPrimitive.Root>
  );
}

function ScriptButton({ onScript }: { onScript: (file: File) => void }) {
  const picker = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={picker}
        type="file"
        hidden
        accept=".txt,.pdf,.fdx,.fountain,.md,text/plain,application/pdf"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onScript(file);
          // Cleared so choosing the same file twice fires change both times —
          // which a producer does after fixing a scan.
          e.target.value = "";
        }}
      />
      <button
        type="button"
        onClick={() => picker.current?.click()}
        className="rounded-md border px-3 py-2 text-sm hover:bg-accent"
        title="Read a screenplay: text, Fountain, Final Draft or PDF"
      >
        Script
      </button>
    </>
  );
}

function Producer() {
  return (
    <MessagePrimitive.Root className="my-3 flex justify-end">
      <Arrives className="max-w-[80%] rounded-lg bg-muted px-4 py-2 text-sm">
        <MessagePrimitive.Parts />
      </Arrives>
    </MessagePrimitive.Root>
  );
}

function Agent() {
  return (
    <MessagePrimitive.Root className="my-3 text-sm">
      <Arrives>
        {/* Answers from `/chat` are plain text with newlines the briefing put
            there on purpose — a list of decisions is a list, not a paragraph. */}
        <MessagePrimitive.Parts
          components={{
            ...TOOLS,
            Text: ({ text }) => <p className="whitespace-pre-wrap">{text}</p>,
          }}
        />
      </Arrives>
    </MessagePrimitive.Root>
  );
}

/**
 * A row easing in rather than appearing.
 *
 * The demo argument for this: most of what lands in this transcript was not
 * typed by anybody. An email to a supplier, a reply that came back days later
 * — they arrive from Firestore while nobody is watching, and a row that pops
 * into existence at full opacity is indistinguishable from one that was always
 * there. The motion is what makes "this happened just now" legible.
 *
 * Short and small on purpose. A long entrance on a transcript that fills
 * itself becomes a screen that will not sit still.
 */
function Arrives({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const still = useReducedMotion();

  if (still) return <div className={className}>{children}</div>;

  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: "easeOut" }}
    >
      {children}
    </motion.div>
  );
}

function Empty() {
  return (
    <div className="mx-auto mt-16 max-w-md text-center text-sm text-muted-foreground">
      <p className="text-base font-medium text-foreground">
        Give the agent a screenplay.
      </p>
      <p className="mt-2">
        Drop one here, or press Script. It reads every physical thing a scene
        needs and shows you the line it found each in, so you can check the
        list rather than trust it.
      </p>
      <p className="mt-2">
        After that, every email it sends and every reply it gets appears here on
        its own. You do not have to be watching.
      </p>
    </div>
  );
}
