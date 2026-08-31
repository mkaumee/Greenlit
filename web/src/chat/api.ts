/**
 * Calls to `cinema-api` — the producer's service, which cannot spend money.
 *
 * A separate file from `approvals.ts` because they are separate services with
 * separate accounts, and the difference is the guardrail. Approvals runs under
 * the one identity with an IAM binding on the `orders` database; this one is
 * conditioned to `(default)` and could not write a purchase order if a bug
 * asked it to. Two clients, so that boundary is visible in the front end too
 * rather than being a fact only the deploy script knows.
 */

import { auth } from "@/firebase";

import type { Reference } from "./rows";

export type Answer =
  | { kind: "answered"; text: string; refs: Reference[]; waitingOnYou: number }
  | { kind: "signed-out"; detail: string }
  | { kind: "not-yours"; detail: string }
  | { kind: "error"; detail: string };

const base = (): string => {
  const url = import.meta.env["VITE_API_URL"];
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
 * Ask the agent about a production.
 *
 * Failures come back as values rather than exceptions, and each kind is
 * distinguished, because they need different things from the person reading
 * the screen. Signed out means sign in again; not-yours means the project is
 * not theirs — which is also what a project that does not exist returns, on
 * purpose, since telling those apart would say whether an id exists.
 */
export async function ask(projectId: string, question: string): Promise<Answer> {
  const url = base();
  if (url === "") {
    return {
      kind: "error",
      detail:
        "VITE_API_URL was not set when this was built, so there is nowhere " +
        "to send the question. `make deploy-web` fills it in.",
    };
  }

  let response: Response;
  try {
    response = await fetch(`${url}/chat`, {
      method: "POST",
      headers: await authorised(),
      body: JSON.stringify({ project_id: projectId, question }),
    });
  } catch (cause) {
    return { kind: "error", detail: describe(cause) };
  }

  if (response.status === 401) {
    return { kind: "signed-out", detail: "Your sign-in expired. Sign in again." };
  }
  if (response.status === 403 || response.status === 404) {
    return {
      kind: "not-yours",
      detail: "That production is not yours, or does not exist.",
    };
  }
  if (!response.ok) {
    return { kind: "error", detail: `${response.status} ${await response.text()}` };
  }

  const body = (await response.json()) as {
    text: string;
    references: Reference[];
    waiting_on_you: number;
  };
  return {
    kind: "answered",
    text: body.text,
    refs: body.references,
    waitingOnYou: body.waiting_on_you,
  };
}

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);
