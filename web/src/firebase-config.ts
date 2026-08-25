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
 *     firebase apps:create web "Greenlit" --project <project-id>
 *     firebase apps:sdkconfig web --project <project-id>
 */

export const firebaseConfig = {
  apiKey: "AIzaSyAk-gfb1xMWv_xpQQsTtpSYUllaFiJwdzo",
  authDomain: "encoded-phalanx-505503-v8.firebaseapp.com",
  projectId: "encoded-phalanx-505503-v8",
  storageBucket: "encoded-phalanx-505503-v8.firebasestorage.app",
  messagingSenderId: "678371873554",
  appId: "1:678371873554:web:ee6bce34a861e1d163d9cb",
} as const;

// `measurementId` from the console block is deliberately absent. It is only
// read by getAnalytics(), which this app does not call and should not: a debug
// panel for two people does not need another SDK or the cookies that come with
// it.

/** The project the emulators run under — see the Makefile's `emulator` target. */
export const EMULATOR_PROJECT = "demo-cinema";

export const isPlaceholder = (): boolean =>
  Object.values(firebaseConfig).some((value) => value.includes("REPLACE_ME"));
