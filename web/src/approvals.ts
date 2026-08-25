/**
 * The one call in this app that spends money.
 *
 * It goes to a different service from everything else on purpose. The tick
 * service that runs the agent has no route that can create a purchase order
 * and no client for the database they live in; approval is a separate Cloud
 * Run service running under the only account with an IAM binding on `orders`.
 * That separation is what makes "the agent cannot spend money" a fact about
 * IAM rather than a claim about a prompt, and it is why this file exists
 * rather than another method on the Firestore hooks.
 *
 * The browser never writes an order directly either — `firestore.rules` denies
 * it, and rules do not apply to server SDKs, which is the whole reason orders
 * live in their own database.
 */

import { auth } from "@/firebase";

export interface Money {
  amount: number;
  currency: string;
}

export type Outcome =
  | { kind: "approved"; price: Money; alreadyExisted: boolean }
  | { kind: "duplicate"; detail: string }
  | { kind: "forbidden"; detail: string }
  | { kind: "conflict"; detail: string }
  | { kind: "error"; detail: string };

const base = (): string => {
  const url = import.meta.env["VITE_APPROVALS_URL"];
  return typeof url === "string" ? url.replace(/\/$/, "") : "";
};

async function authorised(): Promise<Record<string, string>> {
  const user = auth.currentUser;
  if (user === null) throw new Error("not signed in");
  return {
    Authorization: `Bearer ${await user.getIdToken()}`,
    "Content-Type": "application/json",
  };
}

/**
 * Approve one item at one price.
 *
 * The interesting outcome is `duplicate`. A purchase order is created with
 * `create()` keyed by the *item*, so a second order for the same item is
 * refused by the storage engine before any of our code runs — including from a
 * different supplier, which is the same violation. That refusal is worth
 * showing on screen rather than swallowing: it is the most defensible thing
 * this system does.
 */
export async function approve(
  projectId: string,
  itemId: string,
  negotiationId: string,
): Promise<Outcome> {
  if (base() === "") {
    return {
      kind: "error",
      detail:
        "VITE_APPROVALS_URL is not set, so there is nowhere to send the " +
        "approval. Set it to the cinema-approvals service URL and rebuild.",
    };
  }

  let reply: Response;
  try {
    reply = await fetch(`${base()}/items/${itemId}/approve`, {
      method: "POST",
      headers: await authorised(),
      body: JSON.stringify({
        project_id: projectId,
        negotiation_id: negotiationId,
      }),
    });
  } catch (cause) {
    // A failed preflight lands here with a message that mentions neither CORS
    // nor approvals, so say what it usually means.
    return {
      kind: "error",
      detail:
        `could not reach the approval service (${String(cause)}). If this ` +
        "is a CORS failure, CINEMA_ALLOWED_ORIGINS on the service does not " +
        "list this page's origin.",
    };
  }

  const body: unknown = await reply.json().catch(() => ({}));
  const detail = detailOf(body);

  if (reply.ok) {
    const ok = body as { price?: Money; already_existed?: boolean };
    return {
      kind: "approved",
      price: ok.price ?? { amount: 0, currency: "" },
      alreadyExisted: ok.already_existed === true,
    };
  }
  if (reply.status === 403) return { kind: "forbidden", detail };
  if (reply.status === 409) {
    // 409 covers two different things: the guardrail refusing a second order
    // for this item, and a negotiation that has moved on since the screen
    // loaded. The message distinguishes them; the caller shows both.
    return /order|purchase/i.test(detail)
      ? { kind: "duplicate", detail }
      : { kind: "conflict", detail };
  }
  return { kind: "error", detail: detail || `HTTP ${String(reply.status)}` };
}

/** Hand a negotiation back to the agent with a ceiling it may not cross. */
export async function setFloor(
  projectId: string,
  negotiationId: string,
  floor: Money,
): Promise<Outcome> {
  try {
    const reply = await fetch(`${base()}/negotiations/${negotiationId}/floor`, {
      method: "POST",
      headers: await authorised(),
      body: JSON.stringify({ project_id: projectId, floor_price: floor }),
    });
    const body: unknown = await reply.json().catch(() => ({}));
    if (reply.ok) {
      return { kind: "approved", price: floor, alreadyExisted: false };
    }
    if (reply.status === 403) {
      return { kind: "forbidden", detail: detailOf(body) };
    }
    return { kind: "error", detail: detailOf(body) };
  } catch (cause) {
    return { kind: "error", detail: String(cause) };
  }
}

const detailOf = (body: unknown): string => {
  const d = (body as { detail?: unknown } | null)?.detail;
  return typeof d === "string" ? d : "";
};
