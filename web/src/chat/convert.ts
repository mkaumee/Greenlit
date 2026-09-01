/**
 * Turning our rows into what assistant-ui renders.
 *
 * Pure, and separated from the store for that reason: it is the piece where a
 * mistake is invisible. A row converted to the wrong shape does not throw — it
 * renders as an empty bubble, or does not render at all, and the transcript
 * quietly stops being a record of what happened.
 *
 * ## Why an approval is a tool call
 *
 * assistant-ui models "the assistant wants to do something a human must
 * authorise" as a `tool-call` part carrying an `approval`. That is exactly the
 * stop condition this system is built around — the agent negotiates for days
 * and then stops at READY_FOR_HUMAN, because it never buys anything. Reusing
 * the library's own primitive means the decision renders as a decision rather
 * than as a paragraph that mentions one.
 *
 * It is still not the only place the decision lives. See `Chat.tsx`: the same
 * decision appears in a rail count and an inspector card that do not scroll,
 * because a purchase a producer has to scroll back to find is a purchase they
 * will miss.
 */

import type { ThreadMessageLike } from "@assistant-ui/react";

import type { Row } from "./rows";

/**
 * The tool names activities and decisions render under.
 *
 * Exported because the component that renders one and the converter that emits
 * it have to agree, and a mismatch shows up as a message with no body rather
 * than as an error.
 */
export const EMAIL_TOOL = "email";
export const DECISION_TOOL = "approve_purchase";

export function toThreadMessage(row: Row): ThreadMessageLike {
  switch (row.kind) {
    case "producer":
      return {
        id: row.id,
        role: "user",
        content: [{ type: "text", text: row.text }],
        createdAt: row.at,
      };

    case "briefing":
      return {
        id: row.id,
        role: "assistant",
        content: [{ type: "text", text: row.text }],
        createdAt: row.at,
      };

    case "activity":
      // A completed tool call: it already happened, so it carries its result
      // rather than sitting pending. The agent sent this email days ago and
      // nobody is waiting on it.
      return {
        id: row.id,
        role: "assistant",
        content: [
          {
            type: "tool-call",
            toolCallId: row.id,
            toolName: EMAIL_TOOL,
            args: {
              direction: row.direction,
              supplier: row.supplier,
              item: row.itemName,
              subject: row.subject,
            },
            result: { body: row.body },
          },
        ],
        createdAt: row.at,
      };

    case "decision":
      // Deliberately no `result`: this one has not happened and must not look
      // as though it has. The `approval` field is what makes the library
      // render it as a decision awaiting a person.
      return {
        id: row.id,
        role: "assistant",
        content: [
          {
            type: "tool-call",
            toolCallId: row.id,
            toolName: DECISION_TOOL,
            args: {
              itemId: row.itemId,
              item: row.itemName,
              supplier: row.supplier,
              price: row.price,
              rounds: row.roundsUsed,
              reason: row.reason,
              reasoning: row.reasoning,
              rivals: row.rivals,
            },
            approval: { id: row.negotiationId },
          },
        ],
        createdAt: row.at,
      };
  }
}
