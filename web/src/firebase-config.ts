/**
 * The Firebase web app config. Committed on purpose.
 *
 * A Firebase web API key is a public identifier, not a credential. It names
 * the project so the SDK knows where to send requests; it grants nothing. What
 * protects the data is `firestore.rules` and Firebase Auth — which is why that
 * file has its own test suite. Every Firebase web app ships this in its
 * JavaScript bundle, because it has to.
 *
 * So do not "fix" this by moving it into a gitignored `.env`. The only effect
 * would be that a fresh clone builds a page that cannot reach the project, and
 * the hosted build breaks in a way nobody can reproduce locally.
 *
 * To fill it in:
 *
 *     firebase apps:create web "Agentic Cinema" --project <project-id>
 *     firebase apps:sdkconfig web --project <project-id>
 */

export const firebaseConfig = {
  apiKey: "REPLACE_ME",
  authDomain: "REPLACE_ME.firebaseapp.com",
  projectId: "REPLACE_ME",
  appId: "REPLACE_ME",
} as const;

/** The project the emulators run under — see the Makefile's `emulator` target. */
export const EMULATOR_PROJECT = "demo-cinema";

export const isPlaceholder = (): boolean =>
  Object.values(firebaseConfig).some((value) => value.includes("REPLACE_ME"));
