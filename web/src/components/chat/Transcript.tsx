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

import { ComposerPrimitive, MessagePrimitive, ThreadPrimitive } from "@assistant-ui/react";

import { DECISION_TOOL, EMAIL_TOOL } from "@/chat/convert";
import { DecisionPart } from "@/components/chat/DecisionPart";
import { EmailPart } from "@/components/chat/EmailPart";

const TOOLS = {
  tools: {
    by_name: { [EMAIL_TOOL]: EmailPart, [DECISION_TOOL]: DecisionPart },
  },
} as const;

export function Transcript() {
  return (
    <ThreadPrimitive.Root className="flex h-full flex-col">
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-6 py-4">
        <ThreadPrimitive.Empty>
          <Empty />
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <Producer /> : <Agent />
          }
        </ThreadPrimitive.Messages>
      </ThreadPrimitive.Viewport>

      <div className="border-t px-6 py-4">
        <ComposerPrimitive.Root className="flex items-end gap-2">
          <ComposerPrimitive.Input
            autoFocus
            rows={1}
            placeholder="Ask what needs you, who has gone quiet, what things cost…"
            className="max-h-40 flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <ComposerPrimitive.Send className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            Ask
          </ComposerPrimitive.Send>
        </ComposerPrimitive.Root>
      </div>
    </ThreadPrimitive.Root>
  );
}

function Producer() {
  return (
    <MessagePrimitive.Root className="my-3 flex justify-end">
      <div className="max-w-[80%] rounded-lg bg-muted px-4 py-2 text-sm">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function Agent() {
  return (
    <MessagePrimitive.Root className="my-3 text-sm">
      {/* Answers from `/chat` are plain text with newlines the briefing put
          there on purpose — a list of decisions is a list, not a paragraph. */}
      <MessagePrimitive.Parts
        components={{
          ...TOOLS,
          Text: ({ text }) => <p className="whitespace-pre-wrap">{text}</p>,
        }}
      />
    </MessagePrimitive.Root>
  );
}

function Empty() {
  return (
    <div className="mx-auto mt-16 max-w-md text-center text-sm text-muted-foreground">
      <p className="text-base font-medium text-foreground">Nothing has happened yet.</p>
      <p className="mt-2">
        Every email the agent sends and every reply it gets appears here on its
        own, in the order it happened. You do not have to be watching.
      </p>
    </div>
  );
}
