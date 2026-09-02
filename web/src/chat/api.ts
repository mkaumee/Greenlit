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
  | {
      kind: "answered";
      text: string;
      refs: Reference[];
      waitingOnYou: number;
      /** Which half answered. Both are true; only one reasoned. */
      source: "agent" | "stored-facts";
    }
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
    source?: string;
  };
  return {
    kind: "answered",
    text: body.text,
    refs: body.references,
    waitingOnYou: body.waiting_on_you,
    // Anything but an explicit "agent" is treated as the deterministic path,
    // including an older deployment that does not send the field. Claiming the
    // brain answered when it might not have is the mistake worth avoiding.
    source: body.source === "agent" ? "agent" : "stored-facts",
  };
}

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

export type Consent =
  | { kind: "ready"; authorizeUrl: string }
  | { kind: "error"; detail: string };

/**
 * Ask `cinema-api` for a consent URL.
 *
 * The URL is opened in a popup by the caller rather than redirected to from
 * here, so the panel stays where it is: the callback page closes itself and
 * the `mailboxes/{uid}` snapshot is what flips the card. Nothing is posted
 * back, and nothing polls.
 */
export async function startConsent(): Promise<Consent> {
  const url = base();
  if (url === "") {
    return {
      kind: "error",
      detail:
        "VITE_API_URL was not set when this was built, so there is nowhere " +
        "to ask for a consent URL. `make deploy-web` fills it in.",
    };
  }

  const user = auth.currentUser;
  if (user === null) return { kind: "error", detail: "Sign in first." };

  try {
    const response = await fetch(`${url}/mailbox/start`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${await user.getIdToken()}`,
        "Content-Type": "application/json",
      },
    });
    if (!response.ok) {
      return { kind: "error", detail: `${response.status} ${await response.text()}` };
    }
    const body = (await response.json()) as { authorize_url: string };
    return { kind: "ready", authorizeUrl: body.authorize_url };
  } catch (cause) {
    return {
      kind: "error",
      detail: cause instanceof Error ? cause.message : String(cause),
    };
  }
}

// --------------------------------------------------------------------------
// Starting a production
// --------------------------------------------------------------------------

/** One prop the agent found, as offered back for confirmation.
 *
 * A type alias rather than an interface, deliberately: assistant-ui's tool-call
 * `args` must satisfy `ReadonlyJSONValue`, and TypeScript gives type aliases an
 * implicit index signature while interfaces get none. An interface here fails
 * to assign with an error about index signatures that says nothing about the
 * actual constraint. */
export type Prop = {
  item_id: string;
  name: string;
  category: string;
  qty: number;
  consumable: boolean;
  confidence: number;
  scenes: string[];
  /** The script lines it was found in. The receipt. */
  lines: string[];
};

export type Started =
  | { kind: "started"; projectId: string; title: string }
  | { kind: "error"; detail: string };

export type ScriptResult =
  | { kind: "read"; props: Prop[] }
  | { kind: "unreadable"; detail: string }
  | { kind: "error"; detail: string };

export type ConfirmResult =
  | { kind: "confirmed"; confirmed: string[]; abandoned: string[] }
  | { kind: "error"; detail: string };

/**
 * One place every call to `cinema-api` goes through.
 *
 * Failures come back as values rather than exceptions because each caller
 * renders them differently, and because a thrown error inside a React event
 * handler is a blank screen.
 */
async function post(path: string, body: unknown): Promise<Response | string> {
  const url = base();
  if (url === "") {
    return (
      "VITE_API_URL was not set when this was built, so there is nowhere to " +
      "send this. `make deploy-web` fills it in."
    );
  }
  try {
    return await fetch(`${url}${path}`, {
      method: "POST",
      headers: await authorised(),
      body: JSON.stringify(body),
    });
  } catch (cause) {
    return describe(cause);
  }
}

export async function startProject(title: string): Promise<Started> {
  const reply = await post("/projects", { title });
  if (typeof reply === "string") return { kind: "error", detail: reply };
  if (!reply.ok) {
    return { kind: "error", detail: await detailOf(reply) };
  }
  const body = (await reply.json()) as { project_id: string; title: string };
  return { kind: "started", projectId: body.project_id, title: body.title };
}

/** What the browser hands the API: text, or a file it has already read. */
export interface Upload {
  filename: string;
  mimeType: string;
  text?: string;
  contentB64?: string;
}

export async function uploadScript(
  projectId: string,
  upload: Upload,
): Promise<ScriptResult> {
  const reply = await post(`/projects/${projectId}/script`, {
    filename: upload.filename,
    mime_type: upload.mimeType,
    text_content: upload.text ?? "",
    content_b64: upload.contentB64 ?? "",
  });
  if (typeof reply === "string") return { kind: "error", detail: reply };

  // 422 is the file being unreadable rather than the request being wrong, and
  // the message is written for a producer — a scanned PDF says so, and says
  // what to do instead. Rendered as-is.
  if (reply.status === 422) {
    return { kind: "unreadable", detail: await detailOf(reply) };
  }
  if (!reply.ok) return { kind: "error", detail: await detailOf(reply) };

  const body = (await reply.json()) as { props: Prop[] };
  return { kind: "read", props: body.props };
}

export interface Choice {
  item_id: string;
  qty: number;
  include: boolean;
}

export async function confirmProps(
  projectId: string,
  choices: Choice[],
): Promise<ConfirmResult> {
  const reply = await post(`/projects/${projectId}/items/confirm`, {
    items: choices,
  });
  if (typeof reply === "string") return { kind: "error", detail: reply };
  if (!reply.ok) return { kind: "error", detail: await detailOf(reply) };

  const body = (await reply.json()) as {
    confirmed: string[];
    abandoned: string[];
  };
  return {
    kind: "confirmed",
    confirmed: body.confirmed,
    abandoned: body.abandoned,
  };
}

const detailOf = async (reply: Response): Promise<string> => {
  try {
    const body = (await reply.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // A non-JSON body from a proxy or a 502 page. The status is still useful.
  }
  return `${reply.status} ${reply.statusText}`;
};

/** Read a dropped file into base64, which is what the API takes. */
export async function toUpload(file: File): Promise<Upload> {
  const buffer = new Uint8Array(await file.arrayBuffer());
  // Chunked because String.fromCharCode(...bytes) on a multi-megabyte
  // screenplay overflows the argument list and throws.
  let binary = "";
  for (let i = 0; i < buffer.length; i += 8192) {
    binary += String.fromCharCode(...buffer.subarray(i, i + 8192));
  }
  return {
    filename: file.name,
    mimeType: file.type || "application/octet-stream",
    contentB64: btoa(binary),
  };
}
